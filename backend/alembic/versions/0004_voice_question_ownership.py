"""Bind accepted speech to the question heard when speech started."""
from alembic import op
import sqlalchemy as sa
revision='0004_voice_question_ownership'
down_revision='0003_pending_voice'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('viva_connections',sa.Column('pending_question_id',sa.String(),nullable=True))


def downgrade():
    with op.batch_alter_table('viva_connections') as batch:
        batch.drop_column('pending_question_id')
