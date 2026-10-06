import threading

from marketalert.main import OiWorker


def test_worker_queues_results_and_survives_errors():
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("network down")
        return [("NIFTY", None, 25000.0, [], {})]

    w = OiWorker(fetch, period=0.01)
    w.poll()
    w.poll()                                           # error is logged, not raised
    assert w.q.qsize() == 1 and w.last_seconds is not None
    w.start()
    for _ in range(200):
        if w.q.qsize() >= 3:
            break
        threading.Event().wait(0.01)
    w.stop()
    assert w.q.qsize() >= 3
