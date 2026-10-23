"""Durable RAW jobs and isolated Windows spooler calls."""
import multiprocessing
import hashlib
import msvcrt
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime


MAX_JOB = 16 * 1024 * 1024
MAX_CLIENTS = 32
MAX_PENDING = 10000
MAX_PENDING_BYTES = 512 * 1024 * 1024


def spool_job(printer, data, result):
    import win32print
    import win32event
    import win32api

    handle = None
    started = False
    mutex = win32event.CreateMutex(None, False, 'Global\\PrinterOnePrinter_' + hashlib.sha256(printer.casefold().encode()).hexdigest())
    owned = False
    try:
        owned = win32event.WaitForSingleObject(mutex, 0) in (0, 128)
        if not owned:
            raise RuntimeError('Another spooler process is using this printer; inspect pending jobs')
        handle = win32print.OpenPrinter(printer)
        windows_id = win32print.StartDocPrinter(handle, 1, ('PrinterOne RAW', None, 'RAW'))
        started = True
        result.send(('sending', windows_id))
        offset = 0
        while offset < len(data):
            written = win32print.WritePrinter(handle, data[offset:offset + 65536])
            if not written or written > min(65536, len(data) - offset):
                raise IOError('WritePrinter did not confirm the bytes written')
            offset += written
        win32print.EndDocPrinter(handle)
        started = False
        result.send(('submitted', windows_id))
    except Exception as exc:
        result.send(('uncertain', str(exc)))
    finally:
        try:
            if handle is not None:
                try:
                    if started:
                        win32print.AbortPrinter(handle)
                finally:
                    win32print.ClosePrinter(handle)
        finally:
            if owned:
                win32event.ReleaseMutex(mutex)
            win32api.CloseHandle(mutex)
            result.close()


class PrintQueue:
    def __init__(self, directory, log):
        os.makedirs(directory, exist_ok=True)
        self.instance_lock = open(os.path.join(directory, 'server.lock'), 'a+b')
        self.instance_lock.seek(0)
        try:
            msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.instance_lock.close()
            raise RuntimeError('PrinterOne is already running for this user') from None
        self.log = log
        self.lock = threading.RLock()
        self.lifecycle = threading.Lock()
        self.db = sqlite3.connect(os.path.join(directory, 'jobs.sqlite3'),
                                  timeout=15, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, printer TEXT NOT NULL COLLATE NOCASE,
            port INTEGER NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL,
            data BLOB, bytes INTEGER NOT NULL, detail TEXT DEFAULT '', windows_id INTEGER)''')
        self.db.execute("UPDATE jobs SET state='uncertain', detail='Interrupted; inspect labels before resolving' WHERE state='sending'")
        self.db.execute('CREATE INDEX IF NOT EXISTS jobs_printer_state ON jobs(printer,state,id)')
        self.db.execute("DELETE FROM jobs WHERE state IN ('submitted','reviewed') AND datetime(created) < datetime('now','localtime','-30 days')")
        self.db.commit()
        self.stop = threading.Event()
        self.threads = []
        self.clients = set()
        self.sockets = {}
        self.limit = threading.BoundedSemaphore(MAX_CLIENTS)
        self.context = multiprocessing.get_context('spawn')

    def execute(self, sql, args=()):
        with self.lock:
            with self.db:
                return self.db.execute(sql, args).fetchall()

    def recent(self):
        return self.execute("SELECT id,created,printer,state,bytes,windows_id,detail FROM jobs WHERE state='uncertain' OR id IN (SELECT id FROM jobs ORDER BY id DESC LIMIT 200) ORDER BY state='uncertain' DESC,id DESC")

    def resolve(self, job_id):
        self.execute("UPDATE jobs SET state='reviewed', data=NULL, detail='Physically checked by operator; no resend' WHERE id=? AND state='uncertain'", (job_id,))

    def enqueue(self, printer, port, data):
        if not data or len(data) > MAX_JOB:
            raise ValueError('Empty job or job exceeds 16 MiB')
        with self.lock:
            count, size = self.db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM jobs WHERE data IS NOT NULL").fetchone()
            if count >= MAX_PENDING or size + len(data) > MAX_PENDING_BYTES:
                raise ValueError('Persistent queue is full')
            with self.db:
                cursor = self.db.execute('INSERT INTO jobs (printer,port,created,state,data,bytes) VALUES (?,?,?,?,?,?)',
                                         (printer, port, datetime.now().isoformat(timespec='seconds'),
                                          'queued', data, len(data)))
            return cursor.lastrowid

    def start(self, printers):
        with self.lifecycle:
            if any(thread.is_alive() for thread in self.threads):
                self.log('[WARN] Server is running or still stopping')
                return False
            valid = []
            ports = set()
            for row in printers:
                name = str(row.get('printer_name', '')).strip()
                try:
                    port = int(row.get('port', 9100))
                    if not name or not 1 <= port <= 65535 or port in ports:
                        raise ValueError('Missing printer, invalid or duplicate port')
                except (ValueError, TypeError) as exc:
                    self.log(f'[!] Invalid printer configuration: {exc}')
                    return False
                ports.add(port)
                valid.append((name, port))
            if not valid:
                self.log('[!] No printers configured')
                return False
            self.stop = threading.Event()
            self.threads = []
            self.sockets = {}
            names = {name.casefold(): name for name, _ in valid}
            for printer in names.values():
                self._start_thread(self.worker, (printer,))
            for printer, port in valid:
                self._start_thread(self.listen, (printer, port))
            return True

    def _start_thread(self, target, args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self.lock:
            self.threads.append(thread)
            thread.start()

    def listen(self, printer, port):
        while not self.stop.is_set():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.settimeout(1)
                listener.bind(('0.0.0.0', port))
                listener.listen(32)
                with self.lock:
                    if self.stop.is_set():
                        return
                    self.sockets[(printer, port)] = listener
                self.log(f'[OK] Listening on TCP {port}: {printer}')
                while not self.stop.is_set():
                    try:
                        client, address = listener.accept()
                    except socket.timeout:
                        continue
                    if not self.limit.acquire(blocking=False):
                        client.close()
                        self.log('[WARN] TCP connection limit reached')
                        continue
                    with self.lock:
                        if self.stop.is_set():
                            client.close()
                            self.limit.release()
                            return
                        self.clients.add(client)
                        self._start_thread(self.receive, (client, address, printer, port))
                        self.threads = [thread for thread in self.threads if thread.is_alive()]
            except OSError as exc:
                if not self.stop.is_set():
                    self.log(f'[!] Port {port}: {exc}; retrying in 10 seconds')
            finally:
                with self.lock:
                    self.sockets.pop((printer, port), None)
                listener.close()
            self.stop.wait(10)

    def receive(self, client, address, printer, port):
        try:
            chunks = []
            size = 0
            deadline = time.monotonic() + 120
            client.settimeout(15)
            with client:
                while not self.stop.is_set():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Total receive time exceeded; job discarded')
                    chunk = client.recv(min(65536, MAX_JOB + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_JOB:
                        raise ValueError('Job exceeds 16 MiB; job discarded')
                    chunks.append(chunk)
                    client.settimeout(min(8, max(0.01, deadline - time.monotonic())))
                # EOF delimits a RAW job. Timeouts never submit partial labels.
                with self.lock:
                    if size and not self.stop.is_set():
                        job_id = self.enqueue(printer, port, b''.join(chunks))
                    else:
                        return
                self.log(f'[QUEUE] Job {job_id}: {printer}, {size} bytes from {address}')
        except Exception as exc:
            self.log(f'[!] Reception from {address}: {exc}')
        finally:
            with self.lock:
                self.clients.discard(client)
            self.limit.release()

    def worker(self, printer):
        while not self.stop.is_set():
            job_id = None
            try:
                with self.lock:
                    blocked = self.execute("SELECT id FROM jobs WHERE printer=? AND state IN ('uncertain','sending') LIMIT 1", (printer,))
                    rows = [] if blocked else self.execute("SELECT id,data FROM jobs WHERE printer=? AND state='queued' ORDER BY id LIMIT 1", (printer,))
                    if rows and not self.stop.is_set():
                        job_id, data = rows[0]
                        self.execute("UPDATE jobs SET state='sending' WHERE id=?", (job_id,))
                    else:
                        rows = []
                if not rows:
                    self.stop.wait(0.5)
                    continue
                self.submit(job_id, printer, data)
            except Exception as exc:
                if job_id is not None:
                    self.execute("UPDATE jobs SET state='uncertain',detail=? WHERE id=? AND state='sending'", (str(exc)[:500], job_id))
                self.log(f'[!] Queue for {printer}: {exc}')
                self.stop.wait(2)

    def submit(self, job_id, printer, data):
        receiver, sender = self.context.Pipe(duplex=False)
        process = self.context.Process(target=spool_job, args=(printer, data, sender), daemon=True)
        state, detail, windows_id = 'uncertain', 'Spooler interrupted or timed out; inspect labels', None
        started = False
        try:
            process.start()
            started = True
            sender.close()
            deadline = time.monotonic() + 60
            while not self.stop.is_set() and time.monotonic() < deadline:
                if receiver.poll(0.2):
                    status, value = receiver.recv()
                    if status == 'sending':
                        windows_id = value
                        self.execute('UPDATE jobs SET windows_id=? WHERE id=?', (windows_id, job_id))
                    elif status == 'submitted':
                        state, detail, windows_id = status, 'Accepted by Windows; physical printing unconfirmed', value
                        break
                    else:
                        detail = str(value)
                        break
                elif not process.is_alive():
                    break
        except Exception as exc:
            detail = str(exc)
        finally:
            if started:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    # Never permit a successor while the spooler caller survives.
                    self.log(f'[!] Waiting for spooler process for job {job_id} to exit')
                    process.kill()
                    process.join()
                process.close()
            sender.close()
            receiver.close()
            self.execute('UPDATE jobs SET state=?,detail=?,windows_id=?,data=CASE WHEN ? THEN NULL ELSE data END WHERE id=?',
                         (state, detail[:500], windows_id, state == 'submitted', job_id))
            self.log(f'[JOB {job_id}] {state}: {detail}')

    def shutdown(self):
        with self.lifecycle:
            with self.lock:
                self.stop.set()
                sockets = list(self.sockets.values()) + list(self.clients)
                threads = list(self.threads)
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            deadline = time.monotonic() + 4
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            self.log('[STOP] Pending jobs preserved; interrupted jobs require review')
