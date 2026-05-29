import threading

from ghcr.events import EventBus, PrOutcome


def test_publish_with_no_subscribers_is_noop():
    bus = EventBus()
    bus.publish(PrOutcome(repo="o/r", pr_number=1, action="review"))  # no raise


def test_subscribers_receive_published_events():
    bus = EventBus()
    seen_a, seen_b = [], []
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)
    evt = PrOutcome(repo="o/r", pr_number=7, action="review", cost_usd=0.02)
    bus.publish(evt)
    assert seen_a == [evt]
    assert seen_b == [evt]


def test_publish_is_thread_safe():
    bus = EventBus()
    received: list = []
    bus.subscribe(received.append)

    def worker(n):
        for i in range(50):
            bus.publish(PrOutcome(repo="o/r", pr_number=n * 100 + i, action="review"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(received) == 8 * 50
