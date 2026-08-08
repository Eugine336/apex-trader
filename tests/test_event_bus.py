"""Tests for tick.event_bus — thread-safe pub/sub."""

import threading


from tick.event_bus import EventBus


class TestEventBusBasics:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []
        bus.subscribe("test", lambda e: received.append(e))
        bus.publish("test", "hello")
        assert received == ["hello"]

    def test_multiple_subscribers(self):
        bus = EventBus()
        r1, r2 = [], []
        bus.subscribe("evt", lambda e: r1.append(e))
        bus.subscribe("evt", lambda e: r2.append(e))
        bus.publish("evt", 42)
        assert r1 == [42]
        assert r2 == [42]

    def test_publish_to_nonexistent_type(self):
        bus = EventBus()
        bus.publish("nothing", "data")

    def test_unsubscribe(self):
        bus = EventBus()
        received = []
        cb = lambda e: received.append(e)
        bus.subscribe("evt", cb)
        bus.publish("evt", 1)
        bus.unsubscribe("evt", cb)
        bus.publish("evt", 2)
        assert received == [1]

    def test_unsubscribe_nonexistent(self):
        bus = EventBus()
        bus.unsubscribe("evt", lambda e: None)

    def test_subscriber_count(self):
        bus = EventBus()
        assert bus.subscriber_count("evt") == 0
        cb = lambda e: None
        bus.subscribe("evt", cb)
        assert bus.subscriber_count("evt") == 1
        bus.subscribe("evt", lambda e: None)
        assert bus.subscriber_count("evt") == 2

    def test_clear(self):
        bus = EventBus()
        bus.subscribe("a", lambda e: None)
        bus.subscribe("b", lambda e: None)
        bus.clear()
        assert bus.subscriber_count("a") == 0
        assert bus.subscriber_count("b") == 0

    def test_different_event_types_isolated(self):
        bus = EventBus()
        r_a, r_b = [], []
        bus.subscribe("a", lambda e: r_a.append(e))
        bus.subscribe("b", lambda e: r_b.append(e))
        bus.publish("a", "only_a")
        assert r_a == ["only_a"]
        assert r_b == []


class TestEventBusErrorHandling:
    def test_failing_callback_does_not_block_others(self):
        bus = EventBus()
        r = []

        def bad_cb(e):
            raise ValueError("boom")

        bus.subscribe("evt", bad_cb)
        bus.subscribe("evt", lambda e: r.append(e))
        bus.publish("evt", "data")
        assert r == ["data"]


class TestEventBusThreadSafety:
    def test_concurrent_publish_subscribe(self):
        bus = EventBus()
        counter = {"n": 0}
        lock = threading.Lock()

        def cb(e):
            with lock:
                counter["n"] += 1

        def subscriber_thread():
            for _ in range(50):
                bus.subscribe("evt", cb)

        def publisher_thread():
            for i in range(100):
                bus.publish("evt", i)

        threads = [
            threading.Thread(target=subscriber_thread),
            threading.Thread(target=subscriber_thread),
            threading.Thread(target=publisher_thread),
            threading.Thread(target=publisher_thread),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert counter["n"] > 0
