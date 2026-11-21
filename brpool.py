"""In-process stand-in for ProcessPoolExecutor.

sqlglot's optimizer and executor tests ship work to spawned worker processes.
Running that work in-process keeps it visible to tracing and avoids paying for
interpreter start-ups, while the pickle round-trip of arguments, results and
exceptions keeps what crossing a process boundary would exercise.
"""

import concurrent.futures
import pickle


def _rt(obj):
    return pickle.loads(pickle.dumps(obj, protocol=pickle.DEFAULT_PROTOCOL))


class SyncProcessPool(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._initializer = initializer
        self._initargs = initargs
        self._initialized = False

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            if not self._initialized:
                self._initialized = True
                if self._initializer is not None:
                    init, iargs = _rt((self._initializer, self._initargs))
                    init(*iargs)
            fn2, a2, k2 = _rt((fn, args, kwargs))
            r = _rt(fn2(*a2, **k2))
        except Exception as e:  # noqa: BLE001
            try:
                e = _rt(e)
            except Exception:  # noqa: BLE001
                pass
            f.set_exception(e)
        else:
            f.set_result(r)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def install():
    concurrent.futures.ProcessPoolExecutor = SyncProcessPool
    import concurrent.futures.process as p

    p.ProcessPoolExecutor = SyncProcessPool
    _limit_duckdb_threads()


def _limit_duckdb_threads():
    """DuckDB starts one thread per host core for every connection; the run
    is capped at 256 processes (threads count), so keep connections to one."""
    try:
        import duckdb
    except Exception:  # noqa: BLE001
        return
    if getattr(duckdb.connect, "_br_wrapped", False):
        return
    orig = duckdb.connect

    def connect(database=":memory:", read_only=False, config=None, **kwargs):
        config = dict(config or {})
        config.setdefault("threads", 1)
        return orig(database, read_only, config=config, **kwargs)

    connect._br_wrapped = True
    duckdb.connect = connect
