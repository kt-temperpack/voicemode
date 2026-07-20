import pytest

from voice_mode.broker.activation import (
    ActivationBus,
    ActivationEvent,
    ActivationKind,
    ActivationState,
    reduce_activation,
)
from voice_mode.broker.hotkey import (
    HotkeyRegistrationError,
    PlatformHotkeyAdapter,
    TerminalKeyAdapter,
)


def event(kind):
    return ActivationEvent(kind, "test", 1.0)


def test_activation_reducer_has_deterministic_press_release_and_toggle_states():
    state = reduce_activation(
        ActivationState(), event(ActivationKind.PUSH_TO_TALK_PRESS)
    )
    assert state.direct_capture is True
    assert state.push_to_talk_held is True
    assert state.endpoint_requested is False

    state = reduce_activation(state, event(ActivationKind.PUSH_TO_TALK_RELEASE))
    assert state.push_to_talk_held is False
    assert state.endpoint_requested is True

    toggled = reduce_activation(ActivationState(), event(ActivationKind.TOGGLE))
    assert toggled.toggle_active is True
    assert toggled.direct_capture is True
    toggled = reduce_activation(toggled, event(ActivationKind.TOGGLE))
    assert toggled.toggle_active is False
    assert toggled.endpoint_requested is True


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        (ActivationKind.WAKE, "direct_capture"),
        (ActivationKind.SLEEP, "sleep_requested"),
        (ActivationKind.INTERRUPT, "interrupt_requested"),
    ],
)
def test_activation_reducer_exposes_control_events(kind, field):
    assert getattr(reduce_activation(ActivationState(), event(kind)), field) is True


def test_bus_fans_out_without_retaining_an_unbounded_second_copy():
    bus = ActivationBus()
    received = []
    unsubscribe = bus.subscribe(received.append)

    bus.publish(event(ActivationKind.WAKE))
    assert [item.kind for item in received] == [ActivationKind.WAKE]
    assert bus.drain() == []

    unsubscribe()
    bus.publish(event(ActivationKind.SLEEP))
    assert [item.kind for item in bus.drain()] == [ActivationKind.SLEEP]


class FakeListener:
    def __init__(self, *, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def canonical(self, key):
        return key

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeKeyboard:
    Listener = FakeListener

    class HotKey:
        @staticmethod
        def parse(_binding):
            return ["ctrl", "space"]


def test_platform_hotkey_publishes_one_press_and_one_release_per_hold():
    bus = ActivationBus()
    adapter = PlatformHotkeyAdapter(
        "<ctrl>+space", bus, keyboard_module=FakeKeyboard
    )
    adapter.start()

    adapter._listener.on_press("ctrl")
    adapter._listener.on_press("space")
    adapter._listener.on_press("space")
    adapter._listener.on_release("space")

    assert [item.kind for item in bus.drain()] == [
        ActivationKind.PUSH_TO_TALK_PRESS,
        ActivationKind.PUSH_TO_TALK_RELEASE,
    ]
    adapter.close()
    assert adapter._listener is None


def test_hotkey_failure_names_the_exact_reconfiguration_surface():
    class BrokenKeyboard(FakeKeyboard):
        class HotKey:
            @staticmethod
            def parse(_binding):
                raise ValueError("already registered")

    adapter = PlatformHotkeyAdapter(
        "<ctrl>+space", ActivationBus(), keyboard_module=BrokenKeyboard
    )

    with pytest.raises(HotkeyRegistrationError) as raised:
        adapter.start()

    assert "--hotkey" in str(raised.value)
    assert "VOICEMODE_BROKER_HOTKEY" in str(raised.value)
    assert "already registered" in str(raised.value)


def test_terminal_fallback_maps_space_sleep_and_interrupt():
    bus = ActivationBus()
    adapter = TerminalKeyAdapter(bus, read_key=lambda: "")

    for key in [" ", " ", "s", "i", "x"]:
        adapter.feed_key(key)

    assert [item.kind for item in bus.drain()] == [
        ActivationKind.PUSH_TO_TALK_PRESS,
        ActivationKind.PUSH_TO_TALK_RELEASE,
        ActivationKind.SLEEP,
        ActivationKind.INTERRUPT,
    ]
