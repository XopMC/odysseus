"""Validated context settings and one full-request budget calculation.

Backend capacity is evidence supplied by the caller, never granted by a profile.
This module does not resolve owners, grant provider access or start inference.
"""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class ContextPolicy:
    auto_compact: bool = True
    requested_window: int = 0  # zero means use the confirmed backend window
    output_reserve: int = 4096
    safety_tokens: int = 1024
    safety_percent: int = 5
    trigger_percent: int = 75
    target_percent: int = 50
    recent_groups: int = 4
    recent_tokens: int = 2048
    summary_tokens: int = 1200
    summary_timeout_seconds: int = 150

    def __post_init__(self):
        if type(self.auto_compact) is not bool:
            raise ValueError('auto_compact must be a boolean')
        ranges = {
            'requested_window': (0, 2097152), 'output_reserve': (256, 131072),
            'safety_tokens': (0, 131072), 'safety_percent': (0, 50),
            'trigger_percent': (10, 95), 'target_percent': (5, 90),
            'recent_groups': (0, 100), 'recent_tokens': (0, 131072),
            'summary_tokens': (128, 32768), 'summary_timeout_seconds': (5, 600),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name} must be an integer in [{low}, {high}]')
        if self.target_percent >= self.trigger_percent:
            raise ValueError('target_percent must be smaller than trigger_percent')

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError('Unknown context policy fields')
        return cls(**values)

    def to_dict(self):
        return asdict(self)

    def budget(self, backend_window, *, schema_tokens=0, hard_input_max=None):
        for name, value in [('backend_window', backend_window), ('schema_tokens', schema_tokens)]:
            if type(value) is not int or value < (1 if name == 'backend_window' else 0):
                raise ValueError(f'{name} must be a non-negative integer (window must be positive)')
        if hard_input_max is not None and (type(hard_input_max) is not int or hard_input_max <= 0):
            raise ValueError('hard_input_max must be positive')
        window = min(backend_window, self.requested_window or backend_window)
        safety = self.safety_tokens + math.ceil(window * self.safety_percent / 100)
        available = window - self.output_reserve - safety
        if hard_input_max is not None:
            available = min(available, hard_input_max)
        trigger = available * self.trigger_percent // 100
        target = available * self.target_percent // 100
        if available <= 0 or target <= schema_tokens:
            raise ValueError('Context policy leaves no usable input budget after reserves and tool schemas')
        return ContextBudget(window, available, self.output_reserve, safety, schema_tokens,
                             trigger - schema_tokens, target - schema_tokens,
                             available - schema_tokens)


@dataclass(frozen=True)
class ContextBudget:
    window: int
    input_tokens: int  # includes schemas, messages, system prompt and image estimates
    output_reserve: int
    safety_tokens: int
    schema_tokens: int
    trigger_messages: int
    target_messages: int
    hard_messages: int

    def action(self, message_tokens, *, auto_compact=True):
        if type(message_tokens) is not int or message_tokens < 0:
            raise ValueError('message_tokens must be a non-negative integer')
        if message_tokens >= self.trigger_messages and auto_compact:
            return 'compact'
        if message_tokens > self.hard_messages:
            return 'blocked'
        return 'continue'
