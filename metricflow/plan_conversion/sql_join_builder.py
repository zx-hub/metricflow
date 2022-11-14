from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, TypeVar

from metricflow.dataflow.dataflow_plan import JoinDescription, JoinOverTimeRangeNode
from metricflow.plan_conversion.sql_dataset import SqlDataSet
from metricflow.sql.sql_plan import SqlExpressionNode, SqlJoinDescription, SqlJoinType, SqlSelectStatementNode
from metricflow.sql.sql_exprs import (
    SqlColumnReference,
    SqlColumnReferenceExpression,
    SqlComparison,
    SqlComparisonExpression,
    SqlDateTruncExpression,
    SqlIsNullExpression,
    SqlLogicalExpression,
    SqlLogicalOperator,
    SqlTimeDeltaExpression,
)

SqlDataSetT = TypeVar("SqlDataSetT", bound=SqlDataSet)


def _make_validity_window_on_condition(
    left_source_alias: str,
    left_source_time_dimension_name: str,
    right_source_alias: str,
    window_start_dimension_name: str,
    window_end_dimension_name: str,
) -> Tuple[SqlExpressionNode, ...]:
    """Helper to convert a validity window join description to a renderable SQL expresssion

    Takes in a join description consisting of a start and end time dimension spec, and generates
    a node representing the equivalent of this expression:

        {start_dimension_name} >= metric_time AND ({end_dimension_name} < metric_time OR {end_dimension_name} IS NULL)

    """
    window_start_condition = SqlComparisonExpression(
        left_expr=SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=left_source_alias,
                column_name=left_source_time_dimension_name,
            )
        ),
        comparison=SqlComparison.GREATER_THAN_OR_EQUALS,
        right_expr=SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=right_source_alias,
                column_name=window_start_dimension_name,
            )
        ),
    )
    window_end_by_time = SqlComparisonExpression(
        left_expr=SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=left_source_alias,
                column_name=left_source_time_dimension_name,
            )
        ),
        comparison=SqlComparison.LESS_THAN,
        right_expr=SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=right_source_alias,
                column_name=window_end_dimension_name,
            )
        ),
    )
    window_end_is_null = SqlIsNullExpression(
        SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=right_source_alias,
                column_name=window_end_dimension_name,
            )
        )
    )
    window_end_condition = SqlLogicalExpression(
        operator=SqlLogicalOperator.OR, args=(window_end_by_time, window_end_is_null)
    )

    return (window_start_condition, window_end_condition)


@dataclass(frozen=True)
class ColumnEqualityDescription:
    """Helper class to enumerate columns that should be equal between sources in a join.

    The `treat_nulls_as_equal` property determines what to do with null valued inputs. By default SQL returns
    NULL for any equality comparison with a NULL on either side, which means `NULL = NULL` returns NULL, which
    evaluates to False in the join comparison. If that parameter is overridden to True, then the column comparison
    will be rendered as:

        `(left_column_alias = right_column_alias OR (left_column_alias IS NULL AND right_column_alias IS NULL))`
    """

    left_column_alias: str
    right_column_alias: str
    treat_nulls_as_equal: bool = False


@dataclass(frozen=True)
class AnnotatedSqlDataSet:
    """Class to bind a DataSet to transient properties associated with it at a given point in the SqlQueryPlan"""

    data_set: SqlDataSet
    alias: str
    _metric_time_column_name: Optional[str] = None

    @property
    def metric_time_column_name(self) -> str:
        """Direct accessor for the optional metric time name, only safe to call when we know that value is set"""
        assert (
            self._metric_time_column_name
        ), "Expected a valid metric time dimension name to be associated with this dataset, but did not get one!"
        return self._metric_time_column_name


class SqlQueryPlanJoinBuilder:
    """Helper class for constructing various join components in a SqlQueryPlan"""

    @staticmethod
    def make_sql_join_description(
        right_source_node: SqlSelectStatementNode,
        left_source_alias: str,
        right_source_alias: str,
        column_equality_descriptions: Sequence[ColumnEqualityDescription],
        join_type: SqlJoinType,
        additional_on_conditions: Sequence[SqlExpressionNode] = tuple(),
    ) -> SqlJoinDescription:
        """Make a join description where the base condition is a set of equality comparisons between columns.

        Typically the columns in column_equality_descriptions are identifiers we are trying to match,
        although they may include things like dimension partitions or time dimension columns where an
        equality is expected.

        Since the framework currently supports join keys OR condition-free cross joins, this helper will require
        either a non-empty set of column equality conditions or a CROSS JOIN.

        Args:
            right_source_node: node representing the join target, may be either a table or subquery
            left_source_alias: string alias identifier for the join source
            right_source_alias: string alias identifier for the join target
            column_equality_descriptions: set of equality constraints for the ON statement
            join_type: type of SQL join, e.g., LEFT, INNER, etc.
            additional_on_conditions: set of additional constraints to add in the ON statement (via AND)
        """
        assert (
            len(column_equality_descriptions) > 0 or join_type is SqlJoinType.CROSS_JOIN
        ), f"No column equality conditions specified for join with type {join_type} - this may render invalid SQL!"

        and_conditions: List[SqlExpressionNode] = []
        for column_equality_description in column_equality_descriptions:
            left_column = SqlColumnReferenceExpression(
                SqlColumnReference(
                    table_alias=left_source_alias,
                    column_name=column_equality_description.left_column_alias,
                )
            )
            right_column = SqlColumnReferenceExpression(
                SqlColumnReference(
                    table_alias=right_source_alias,
                    column_name=column_equality_description.right_column_alias,
                )
            )
            column_equality_expression = SqlComparisonExpression(
                left_expr=left_column,
                comparison=SqlComparison.EQUALS,
                right_expr=right_column,
            )
            if column_equality_description.treat_nulls_as_equal:
                null_comparison_expression = SqlLogicalExpression(
                    operator=SqlLogicalOperator.AND,
                    args=(SqlIsNullExpression(arg=left_column), SqlIsNullExpression(arg=right_column)),
                )
                and_conditions.append(
                    SqlLogicalExpression(
                        operator=SqlLogicalOperator.OR, args=(column_equality_expression, null_comparison_expression)
                    )
                )
            else:
                and_conditions.append(column_equality_expression)

        and_conditions += additional_on_conditions

        on_condition: Optional[SqlExpressionNode]
        if len(and_conditions) == 0:
            on_condition = None
        elif len(and_conditions) == 1:
            on_condition = and_conditions[0]
        else:
            on_condition = SqlLogicalExpression(operator=SqlLogicalOperator.AND, args=tuple(and_conditions))

        return SqlJoinDescription(
            right_source=right_source_node,
            right_source_alias=right_source_alias,
            on_condition=on_condition,
            join_type=join_type,
        )

    @staticmethod
    def make_base_output_join_description(
        annotated_left_data_set: AnnotatedSqlDataSet,
        annotated_right_data_set: AnnotatedSqlDataSet,
        join_description: JoinDescription,
    ) -> SqlJoinDescription:
        """Make a join description to link two base output DataSets by matching identifiers

        In addition to the identifier equality condition, this will ensure datasets are joined on all partition
        columns and account for validity windows, if those are defined in one of the datasets.
        """
        from_data_set = annotated_left_data_set.data_set
        from_data_set_alias = annotated_left_data_set.alias
        right_data_set = annotated_right_data_set.data_set
        right_data_set_alias = annotated_right_data_set.alias

        join_on_identifier = join_description.join_on_identifier

        # Figure out which columns in the "from" data set correspond to the identifier that we want to join on.
        # The column associations tell us which columns correspond to which instances in the data set.
        from_data_set_identifier_column_associations = from_data_set.column_associations_for_identifier(
            join_on_identifier
        )
        from_data_set_identifier_cols = [c.column_name for c in from_data_set_identifier_column_associations]

        # Figure out which columns in the "right" data set correspond to the identifier that we want to join on.
        right_data_set_column_associations = right_data_set.column_associations_for_identifier(join_on_identifier)
        right_data_set_identifier_cols = [c.column_name for c in right_data_set_column_associations]

        assert len(from_data_set_identifier_cols) == len(
            right_data_set_identifier_cols
        ), f"Cannot construct join - the number of columns on the left ({from_data_set_identifier_cols}) side of the join does not match the right ({right_data_set_identifier_cols})"

        # We have the columns that we need to "join on" in the query, so add it to the list of join descriptions to
        # use later.
        column_equality_descriptions = []
        for idx in range(len(from_data_set_identifier_cols)):
            column_equality_descriptions.append(
                ColumnEqualityDescription(
                    left_column_alias=from_data_set_identifier_cols[idx],
                    right_column_alias=right_data_set_identifier_cols[idx],
                )
            )
        # Add the partition columns as well.
        for dimension_join_description in join_description.join_on_partition_dimensions:
            column_equality_descriptions.append(
                ColumnEqualityDescription(
                    left_column_alias=from_data_set.column_association_for_dimension(
                        dimension_join_description.start_node_dimension_spec
                    ).column_name,
                    right_column_alias=right_data_set.column_association_for_dimension(
                        dimension_join_description.node_to_join_dimension_spec
                    ).column_name,
                )
            )

        for time_dimension_join_description in join_description.join_on_partition_time_dimensions:
            column_equality_descriptions.append(
                ColumnEqualityDescription(
                    left_column_alias=from_data_set.column_association_for_time_dimension(
                        time_dimension_join_description.start_node_time_dimension_spec
                    ).column_name,
                    right_column_alias=right_data_set.column_association_for_time_dimension(
                        time_dimension_join_description.node_to_join_time_dimension_spec
                    ).column_name,
                )
            )

        if join_description.validity_window is None:
            validity_conditions: Tuple[SqlExpressionNode, ...] = tuple()
        else:
            # The join to base output node requires a metric_time dimension to process a join against an
            # a dataset with a validity window specified, as that dataset represents an SCD data source
            # We fetch the metric time instance with the smallest granularity and shortest identifier link path,
            # since this will be used in the ON statement for the join against the validity window.
            from_data_set_metric_time_dimension_instances = sorted(
                from_data_set.metric_time_dimension_instances,
                key=lambda x: (x.spec.time_granularity.to_int(), len(x.spec.identifier_links)),
            )
            assert from_data_set_metric_time_dimension_instances, (
                f"Cannot process join to data set with alias {right_data_set_alias} because it has a validity "
                f"window set: {join_description.validity_window}, but source data set with alias "
                f"{from_data_set_alias} does not have a metric time dimension we can use for the window join!"
            )
            from_data_set_time_dimension_name = from_data_set.column_association_for_time_dimension(
                from_data_set_metric_time_dimension_instances[0].spec,
            ).column_name
            window_start_dimension_name = right_data_set.column_association_for_time_dimension(
                join_description.validity_window.window_start_dimension
            ).column_name
            window_end_dimension_name = right_data_set.column_association_for_time_dimension(
                join_description.validity_window.window_end_dimension
            ).column_name
            validity_conditions = _make_validity_window_on_condition(
                left_source_alias=from_data_set_alias,
                left_source_time_dimension_name=from_data_set_time_dimension_name,
                right_source_alias=right_data_set_alias,
                window_start_dimension_name=window_start_dimension_name,
                window_end_dimension_name=window_end_dimension_name,
            )

        return SqlQueryPlanJoinBuilder.make_sql_join_description(
            right_source_node=right_data_set.sql_select_node,
            left_source_alias=from_data_set_alias,
            right_source_alias=right_data_set_alias,
            column_equality_descriptions=column_equality_descriptions,
            join_type=SqlJoinType.LEFT_OUTER,
            additional_on_conditions=validity_conditions,
        )

    @staticmethod
    def make_cumulative_metric_time_range_join_description(
        node: JoinOverTimeRangeNode[SqlDataSetT],
        metric_data_set: AnnotatedSqlDataSet,
        time_spine_data_set: AnnotatedSqlDataSet,
    ) -> SqlJoinDescription:
        """Make a join description to connect a cumulative metric input to a time spine dataset

        Cumulative metrics must be joined against a time spine in a backward-looking fashion, with
        a range determined by a time window (delta against metric_time) and optional cumulative grain.
        """

        # Build an expression like "a.ds <= b.ds AND a.ds >= b.ds - <window>
        # If no window is present we join across all time -> "a.ds <= b.ds"
        metric_time_column_expr = SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=metric_data_set.alias,
                column_name=metric_data_set.metric_time_column_name,
            )
        )
        time_spine_column_expr = SqlColumnReferenceExpression(
            SqlColumnReference(
                table_alias=time_spine_data_set.alias,
                column_name=time_spine_data_set.metric_time_column_name,
            )
        )

        # Comparison expression against the endpoint of the cumulative time range,
        # meaning the metric time must always be BEFORE the time spine time
        end_of_range_comparison_expression = SqlComparisonExpression(
            left_expr=metric_time_column_expr,
            comparison=SqlComparison.LESS_THAN_OR_EQUALS,
            right_expr=time_spine_column_expr,
        )

        comparison_expressions: List[SqlComparisonExpression] = [end_of_range_comparison_expression]
        if node.window:
            start_of_range_comparison_expr = SqlComparisonExpression(
                left_expr=metric_time_column_expr,
                comparison=SqlComparison.GREATER_THAN,
                right_expr=SqlTimeDeltaExpression(
                    arg=time_spine_column_expr,
                    count=node.window.count,
                    granularity=node.window.granularity,
                ),
            )
            comparison_expressions.append(start_of_range_comparison_expr)
        elif node.grain_to_date:
            start_of_range_comparison_expr = SqlComparisonExpression(
                left_expr=metric_time_column_expr,
                comparison=SqlComparison.GREATER_THAN_OR_EQUALS,
                right_expr=SqlDateTruncExpression(arg=time_spine_column_expr, time_granularity=node.grain_to_date),
            )
            comparison_expressions.append(start_of_range_comparison_expr)

        cumulative_join_condition = SqlLogicalExpression(
            operator=SqlLogicalOperator.AND,
            args=tuple(comparison_expressions),
        )

        return SqlJoinDescription(
            right_source=metric_data_set.data_set.sql_select_node,
            right_source_alias=metric_data_set.alias,
            on_condition=cumulative_join_condition,
            join_type=SqlJoinType.INNER,
        )
