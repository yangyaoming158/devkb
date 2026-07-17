"""轨迹表复合外键（T12 复评修复，D7/ADR-0005 项目归属一致性）。

0002 的三个独立 FK 只保证"目标行存在"，挡不住两类错配写入：
项目 A 的 agent_step 引用项目 B 的 run；run A 的 tool_invocation 引用 run B 的 step。
本迁移改为复合 FK，让错配在数据库层直接 IntegrityError：

- agent_steps(run_id, project_id)   → agent_runs(id, project_id)
- tool_invocations(run_id, project_id) → agent_runs(id, project_id)
- tool_invocations(step_id, run_id) → agent_steps(id, run_id)

被引用侧需要的 (id, project_id) / (id, run_id) 唯一约束一并创建。
project_id → projects 的直接 FK 保留（项目删除的直接级联路径）。
两张轨迹表当前无生产数据，无需数据修复。
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_agent_runs_id_project", "agent_runs", ["id", "project_id"])
    op.create_unique_constraint("uq_agent_steps_id_run", "agent_steps", ["id", "run_id"])

    op.drop_constraint("agent_steps_run_id_fkey", "agent_steps", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_steps_run_project",
        "agent_steps",
        "agent_runs",
        ["run_id", "project_id"],
        ["id", "project_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("tool_invocations_run_id_fkey", "tool_invocations", type_="foreignkey")
    op.drop_constraint("tool_invocations_step_id_fkey", "tool_invocations", type_="foreignkey")
    op.create_foreign_key(
        "fk_tool_invocations_run_project",
        "tool_invocations",
        "agent_runs",
        ["run_id", "project_id"],
        ["id", "project_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tool_invocations_step_run",
        "tool_invocations",
        "agent_steps",
        ["step_id", "run_id"],
        ["id", "run_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_tool_invocations_step_run", "tool_invocations", type_="foreignkey")
    op.drop_constraint("fk_tool_invocations_run_project", "tool_invocations", type_="foreignkey")
    op.create_foreign_key(
        "tool_invocations_step_id_fkey",
        "tool_invocations",
        "agent_steps",
        ["step_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "tool_invocations_run_id_fkey",
        "tool_invocations",
        "agent_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_agent_steps_run_project", "agent_steps", type_="foreignkey")
    op.create_foreign_key(
        "agent_steps_run_id_fkey",
        "agent_steps",
        "agent_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("uq_agent_steps_id_run", "agent_steps", type_="unique")
    op.drop_constraint("uq_agent_runs_id_project", "agent_runs", type_="unique")
