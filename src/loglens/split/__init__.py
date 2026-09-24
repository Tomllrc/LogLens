"""Record splitting: group multiline records (e.g. stack traces) into single events."""

from loglens.split.models import Record, SplitOptions, SplitReport
from loglens.split.records import iter_records
from loglens.split.signature import Signature, learn_signature

__all__ = [
    "Record",
    "Signature",
    "SplitOptions",
    "SplitReport",
    "iter_records",
    "learn_signature",
]
