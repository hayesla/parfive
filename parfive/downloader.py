import asyncio
import contextlib
import urllib.parse
from functools import partial
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from tqdm import tqdm, tqdm_notebook

from .results import Results
from .utils import (FailedDownload, Token, default_name, in_notebook,
                    run_in_thread, get_ftp_size, get_http_size, get_filepath)

try:
    import aioftp
except ImportError:
    aioftp = None


__all__ = ['Downloader']


class Downloader:
    """
    Download files in parallel.

    Parameters
    ----------

    max_conn : `int`, optional
        The number of parallel download slots.

    progress : `bool`, optional
        If `True` show a main progress bar showing how many of the total files
        have been downloaded. If `False`, no progress bars will be shown at all.

    file_progress : `bool`, optional
        If `True` and ``progress`` is true, show ``max_conn`` progress bars
        detailing the progress of each individual file being downloaded.

    loop : `asyncio.AbstractEventLoop`, optional
        The event loop to use to download the files. If not specified a new
        loop will be created and executed in a new thread so it does not
        interfere with any currently running event loop.

    notebook : `bool`, optional
        If `True` tqdm will be used in notebook mode. If `None` an attempt will
        be made to detect the notebook and guess which progress bar to use.

    overwrite : `bool` or `str`, optional
        Determine how to handle downloading if a file already exists with the
        same name. If `False` the file download will be skipped and the path
        returned to the existing file, if `True` the file will be downloaded
        and the existing file will be overwritten, if `'unique'` the filename
        will be modified to be unique.
    """

    def __init__(self, max_conn=5, progress=True, file_progress=True,
                 loop=None, notebook=None, overwrite=False):

        self.max_conn = 5
        self._start_loop(loop)

        # Configure progress bars
        if notebook is None:
            notebook = in_notebook()
        self.progress = progress
        self.file_progress = file_progress if self.progress else False
        self.tqdm = tqdm if not notebook else tqdm_notebook

        self.overwrite = overwrite

    def _start_loop(self, loop):
        # Setup asyncio loops
        if not loop:
            aio_pool = ThreadPoolExecutor(1)
            self.loop = asyncio.new_event_loop()
            self.run_until_complete = partial(run_in_thread, aio_pool, self.loop)
        else:
            self.loop = loop
            self.run_until_complete = self.loop.run_until_complete

        # Setup queues
        self.http_queue = asyncio.Queue(loop=self.loop)
        self.http_tokens = asyncio.Queue(maxsize=self.max_conn, loop=self.loop)
        self.ftp_queue = asyncio.Queue(loop=self.loop)
        self.ftp_tokens = asyncio.Queue(maxsize=self.max_conn, loop=self.loop)
        for i in range(self.max_conn):
            self.http_tokens.put_nowait(Token(i + 1))
            self.ftp_tokens.put_nowait(Token(i + 1))

    def enqueue_file(self, url, path=None, filename=None, overwrite=None, **kwargs):
        """
        Add a file to the download queue.

        Parameters
        ----------

        url : `str`
            The URL to retrieve.

        path : `str`
            The directory to retrieve the file into.

        filename : `str` or `callable`
            The filename to save the file as. Can also be a callable which
            takes two arguments the url and the response object from opening
            that URL, and returns the filename. (Note, for FTP downloads the
            response will be ``None``.)

        chunksize : `int`
            The size (in bytes) of the chunks to be downloaded for HTTP downloads.

        overwrite : `bool` or `str`, optional
            Determine how to handle downloading if a file already exists with the
            same name. If `False` the file download will be skipped and the path
            returned to the existing file, if `True` the file will be downloaded
            and the existing file will be overwritten, if `'unique'` the filename
            will be modified to be unique.

        kwargs : `dict`
            Extra keyword arguments are passed to `aiohttp.ClientSession.get`
            or `aioftp.ClientSession` depending on the protocol.
        """
        overwrite = overwrite or self.overwrite

        if path is None and filename is None:
            raise ValueError("either path or filename must be specified.")
        if not filename:
            filepath = partial(default_name, path)
        elif callable(filename):
            filepath = filename
        else:
            # Define a function because get_file expects a callback
            def filepath(*args):
                return filename

        if url.startswith("http"):
            get_file = partial(self._get_http, url=url, filepath_partial=filepath,
                               overwrite=overwrite, **kwargs)
            self.http_queue.put_nowait(get_file)
        elif url.startswith("ftp"):
            if aioftp is None:
                raise ValueError("The aioftp package must be installed to download over FTP")
            get_file = partial(self._get_ftp, url=url, filepath_partial=filepath,
                               overwrite=overwrite, **kwargs)
            self.ftp_queue.put_nowait(get_file)
        else:
            raise ValueError("url must start with either http or ftp")

    def download(self):
        """
        Download all files in the queue.

        Returns
        -------
        filenames : `parfive.Results`
            A list of files downloaded.
        """
        try:
            future = self.run_until_complete(self._run_download())
        finally:
            self.loop.stop()
        dlresults = future.result()

        results = Results()

        # Iterate through the results and store any failed download errors in
        # the errors list of the results object.
        for res in dlresults:
            if isinstance(res, FailedDownload):
                results.add_error(res.filepath_partial, res.url, res.response)
            elif isinstance(res, Exception):
                raise res
            else:
                results.append(res)

        return results

    def retry(self, results):
        """
        Retry any failed downloads in a results object.

        .. note::
            This will start a new event loop.

        Parameters
        ----------

        results : `parfive.Results`
            A previous results object, the ``.errors`` property will be read
            and the downloads retried.

        Returns
        -------

        results : `parfive.Results`
            A modified version of the input results with all the errors from
            this download attempt and any new files appended to the list of
            file paths.

        """
        # Restart the loop.
        self._start_loop(None)

        for err in results.errors:
            self.enqueue_file(err.url, filename=err.filepath_partial)

        new_res = self.download()

        results += new_res
        results._errors = new_res._errors

        return results

    def _get_main_pb(self, total):
        """
        Return the tqdm instance if we want it, else return a contextmanager
        that just returns None.
        """
        if self.progress:
            return self.tqdm(total=total, unit='file',
                             desc="Files Downloaded",
                             position=0)
        else:
            return contextlib.contextmanager(lambda: iter([None]))()

    async def _run_download(self):
        """
        Download all files in the queue.

        Returns
        -------

        results : `parfive.Results`
            A list of filenames which successfully downloaded. This list also
            has an attribute ``errors`` which lists any failed urls and their
            error.
        """
        total_files = self.http_queue.qsize() + self.ftp_queue.qsize()
        done = set()
        with self._get_main_pb(total_files) as main_pb:
            if not self.http_queue.empty():
                done.update(await self._run_http_download(main_pb))
            if not self.ftp_queue.empty():
                done.update(await self._run_ftp_download(main_pb))

        # Return one future to represent all the results.
        return asyncio.gather(*done, return_exceptions=True)

    async def _run_from_queue(self, queue, tokens, main_pb, session=None):
        futures = []
        while not queue.empty():
            get_file = await queue.get()
            token = await tokens.get()
            file_pb = self.tqdm if self.file_progress else False
            future = asyncio.ensure_future(get_file(session, token=token, file_pb=file_pb))

            def callback(token, future, main_pb):
                tokens.put_nowait(token)
                # Update the main progressbar
                if main_pb:
                    main_pb.update(1)

            future.add_done_callback(partial(callback, token, main_pb=main_pb))
            futures.append(future)

        return futures

    async def _run_ftp_download(self, main_pb):
        futures = await self._run_from_queue(self.ftp_queue, self.ftp_tokens, main_pb)
        # Wait for all the coroutines to finish
        done, _ = await asyncio.wait(futures)

        return done

    async def _run_http_download(self, main_pb):
        async with aiohttp.ClientSession(loop=self.loop) as session:
            futures = await self._run_from_queue(self.http_queue, self.http_tokens,
                                                 main_pb, session)

            # Wait for all the coroutines to finish
            done, _ = await asyncio.wait(futures)

            return done

    @staticmethod
    async def _get_http(session, *, url, filepath_partial, chunksize=100,
                        file_pb=None, token, overwrite, **kwargs):
        """
        Read the file from the given url into the filename given by ``filepath_partial``.

        Parameters
        ----------

        session : `aiohttp.ClientSession`
            The `aiohttp.ClientSession` to use to retrieve the files.

        url : `str`
            The url to retrieve.

        filepath_partial : `callable`
            A function to call which returns the filepath to save the url to.
            Takes two arguments ``resp, url``.

        chunksize : `int`
            The number of bytes to read into the file at a time.

        file_pb : `tqdm.tqdm` or `False`
            Should progress bars be displayed for each file downloaded.

        token : `parfive.downloader.Token`
            A token for this download slot.

        kwargs : `dict`
            Extra keyword arguments are passed to `~aiohttp.ClientSession.get`.

        Returns
        -------

        filepath : `str`
            The name of the file saved.

        """
        # Set the socket read timeout to 60s
        timeout = aiohttp.ClientTimeout(total=5 * 60, sock_read=60)
        try:
            async with session.get(url, timeout=timeout, **kwargs) as resp:
                if resp.status != 200:
                    raise FailedDownload(filepath_partial, url, resp)
                else:
                    filepath, skip = get_filepath(filepath_partial(resp, url), overwrite)
                    if skip:
                        return str(filepath)
                    if callable(file_pb):
                        file_pb = file_pb(position=token.n, unit='B', unit_scale=True,
                                          desc=filepath.name, leave=False,
                                          total=get_http_size(resp))
                    else:
                        file_pb = None
                    with open(str(filepath), 'wb') as fd:
                        while True:
                            chunk = await resp.content.read(chunksize)
                            if not chunk:
                                # Close the file progressbar
                                if file_pb is not None:
                                    file_pb.close()

                                return str(filepath)

                            # Write this chunk to the output file.
                            fd.write(chunk)

                            # Update the progressbar for file
                            if file_pb is not None:
                                file_pb.update(chunksize)

        # Catch all the possible aiohttp errors, which are variants on failed
        # downloads and then send them to the user in the place of the response
        # object.
        except aiohttp.ClientError as e:
            raise FailedDownload(filepath_partial, url, e)

    @staticmethod
    async def _get_ftp(session=None, *, url, filepath_partial,
                       file_pb=None, token, overwrite, **kwargs):
        """
        Read the file from the given url into the filename given by ``filepath_partial``.

        Parameters
        ----------

        session : `None`
            A placeholder for API compatibility with ``_get_http``

        url : `str`
            The url to retrieve.

        filepath_partial : `callable`
            A function to call which returns the filepath to save the url to.
            Takes two arguments ``resp, url``.

        file_pb : `tqdm.tqdm` or `False`
            Should progress bars be displayed for each file downloaded.

        token : `parfive.downloader.Token`
            A token for this download slot.

        kwargs : `dict`
            Extra keyword arguments are passed to `~aioftp.ClientSession`.

        Returns
        -------

        filepath : `str`
            The name of the file saved.

        """
        parse = urllib.parse.urlparse(url)
        try:
            async with aioftp.ClientSession(parse.hostname, **kwargs) as client:
                if parse.username and parse.password:
                    client.login(parse.username, parse.password)

                # This has to be done before we start streaming the file:
                total_size = await get_ftp_size(client, parse.path)
                async with client.download_stream(parse.path) as stream:
                    filepath, skip = get_filepath(filepath_partial(None, url), overwrite)
                    if skip:
                        return str(filepath)
                    if callable(file_pb):
                        file_pb = file_pb(position=token.n, unit='B', unit_scale=True,
                                          desc=filepath.name, leave=False, total=total_size)
                    else:
                        file_pb = None

                    with open(str(filepath), 'wb') as fd:
                        async for chunk in stream.iter_by_block():
                            # Write this chunk to the output file.
                            fd.write(chunk)

                            # Update the progressbar for file
                            if file_pb is not None:
                                file_pb.update(len(chunk))

                        # Close the file progressbar
                        if file_pb is not None:
                            file_pb.close()

                        return str(filepath)

        # Catch all the possible aioftp errors, and socket errors (when a
        # server is not found) which are variants on failed downloads and then
        # send them to the user in the place of the response object.
        except (aioftp.StatusCodeError, OSError) as e:
            raise FailedDownload(filepath_partial, url, e)
