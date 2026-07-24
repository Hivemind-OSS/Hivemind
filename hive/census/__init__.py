"""hive-census: composer of unsigned, fail-closed change receipts.

The public surface grows as composer stages land; today it exposes the
versioned receipt schema and its enforcement.
"""

from hive.census.diff import (
    Change,
    ChangedFile,
    ChangeSet,
    open_change,
    parse_unified_diff,
)
from hive.census.engines import attribute_symbols, build_graphs, node_change
from hive.census.envelope import unsigned_envelope
from hive.census.execution import ExecutionClassLine, coerce_execution
from hive.census.join import RegressionFinding, regression_join
from hive.census.memory import (
    MemoryContext,
    fetch_institutional_context,
    order_subjects,
)
from hive.census.precision import PrecisionEntry, assess_precision
from hive.census.propagation import propagation_block
from hive.census.receipt import build_receipt
from hive.census.schema import SCHEMA_VERSION, load_schema, validate_receipt

__all__ = [
    "Change",
    "ChangeSet",
    "ChangedFile",
    "ExecutionClassLine",
    "MemoryContext",
    "PrecisionEntry",
    "RegressionFinding",
    "SCHEMA_VERSION",
    "assess_precision",
    "attribute_symbols",
    "build_graphs",
    "build_receipt",
    "coerce_execution",
    "fetch_institutional_context",
    "load_schema",
    "node_change",
    "open_change",
    "order_subjects",
    "parse_unified_diff",
    "propagation_block",
    "regression_join",
    "unsigned_envelope",
    "validate_receipt",
]
