from functools import partial, wraps

import django
from django.conf import settings
from django.contrib.postgres.indexes import (
    BrinIndex, BTreeIndex, GinIndex, GistIndex, HashIndex, SpGistIndex
)
from django.db import connection, models
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as CoreDatabaseSchemaEditor
)
from django.test import override_settings
from django.utils.module_loading import import_string

import pytest

from django_zero_downtime_migrations.backends.postgres.schema import (
    UnsafeOperationException, UnsafeOperationWarning
)
from tests import skip_for_default_django_backend

if django.VERSION[:2] >= (3, 0):
    from django.contrib.postgres.constraints import ExclusionConstraint

pytestmark = skip_for_default_django_backend

DatabaseSchemaEditor = import_string(settings.DATABASES['default']['ENGINE'] + '.schema.DatabaseSchemaEditor')


PG_VERSION_12 = 120000
PG_VERSION_11 = 110000
START_TIMEOUTS = [
    'SET statement_timeout TO \'0\';',
    'SET lock_timeout TO \'0\';',
]
END_TIMEOUTS = [
    'SET statement_timeout TO \'0ms\';',
    'SET lock_timeout TO \'0ms\';',
]
START_FLEXIBLE_STATEMENT_TIMEOUT = [
    'SET statement_timeout TO \'0ms\';',
]
END_FLEXIBLE_STATEMENT_TIMEOUT = [
    'SET statement_timeout TO \'0ms\';',
]


def timeouts(statements):
    if isinstance(statements, str):
        statements = [statements]
    return START_TIMEOUTS + statements + END_TIMEOUTS


def flexible_statement_timeout(statements):
    if isinstance(statements, str):
        statements = [statements]
    return START_FLEXIBLE_STATEMENT_TIMEOUT + statements + END_FLEXIBLE_STATEMENT_TIMEOUT


def old_pg(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        old = connection.pg_version
        connection.pg_version = PG_VERSION_11
        try:
            result = func(*args, **kwargs)
        finally:
            connection.pg_version = old
        return result
    return wrapper


class Model(models.Model):
    field1 = models.IntegerField()
    field2 = models.IntegerField()


class Model2(models.Model):
    pass


connection.pg_version = PG_VERSION_12
schema_editor = partial(DatabaseSchemaEditor, connection=connection, collect_sql=True)


class cmp_schema_editor:
    schema_editor = DatabaseSchemaEditor
    core_schema_editor = CoreDatabaseSchemaEditor

    def __enter__(self):
        self.editor = self.schema_editor(connection=connection, collect_sql=True).__enter__()
        self.core_editor = self.core_schema_editor(connection=connection, collect_sql=True, atomic=False).__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.core_editor.__exit__(exc_type, exc_value, traceback)
        self.editor.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, item):
        self.method = item
        value = getattr(self.editor, self.method)
        if callable(value):
            return self
        return value

    def __call__(self, *args, **kwargs):
        getattr(self.core_editor, self.method)(*args, **kwargs)
        return getattr(self.editor, self.method)(*args, **kwargs)

    @property
    def django_sql(self):
        return self.core_editor.collected_sql


@pytest.fixture(autouse=True)
def zero_timeouts():
    with override_settings(ZERO_DOWNTIME_MIGRATIONS_LOCK_TIMEOUT=0):
        with override_settings(ZERO_DOWNTIME_MIGRATIONS_STATEMENT_TIMEOUT=0):
            yield


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_create_model__ok():
    with cmp_schema_editor() as editor:
        editor.create_model(Model)
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == [
        'CREATE TABLE "tests_model" '
        '("id" serial NOT NULL PRIMARY KEY, "field1" integer NOT NULL, "field2" integer NOT NULL);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_model__ok():
    with cmp_schema_editor() as editor:
        editor.delete_model(Model)
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == [
        'DROP TABLE "tests_model" CASCADE;',
    ]


def test_rename_model__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER TABLE RENAME is unsafe operation'):
            editor.alter_db_table(Model, 'old_name', 'new_name')
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "old_name" RENAME TO "new_name";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_rename_model__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER TABLE RENAME is unsafe operation'):
            editor.alter_db_table(Model, 'old_name', 'new_name')
    assert editor.django_sql == [
        'ALTER TABLE "old_name" RENAME TO "new_name";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_rename_model_with_same_db_table__ok():
    with cmp_schema_editor() as editor:
        editor.alter_db_table(Model, 'same_table', 'same_table')
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == []


def test_change_model_tablespace__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER TABLE SET TABLESPACE is unsafe operation'):
            editor.alter_db_tablespace(Model, 'old_tablespace', 'new_tablespace')
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" SET TABLESPACE "new_tablespace";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_change_model_tablespace__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER TABLE SET TABLESPACE is unsafe operation'):
            editor.alter_db_tablespace(Model, 'old_tablespace', 'new_tablespace')
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" SET TABLESPACE "new_tablespace";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40, null=True)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL;'
    ]


def test_add_field_with_default__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN DEFAULT is unsafe operation'):
            field = models.CharField(max_length=40, default='test', null=True)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) DEFAULT \'test\' NULL;'
    ) + timeouts(
        'ALTER TABLE "tests_model" ALTER COLUMN "field" DROP DEFAULT;'
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) DEFAULT \'test\' NULL;',
        'ALTER TABLE "tests_model" ALTER COLUMN "field" DROP DEFAULT;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field_with_default__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN DEFAULT is unsafe operation'):
            field = models.CharField(max_length=40, default='test', null=True)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) DEFAULT \'test\' NULL;',
        'ALTER TABLE "tests_model" ALTER COLUMN "field" DROP DEFAULT;',
    ]


def test_add_field_with_not_null__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field_with_not_null__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=True)
def test_add_field_with_not_null__allowed_for_all_tables__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=10)
def test_add_field_with_not_null__allowed_for_small_tables__warning(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=1)
def test_add_field_with_not_null__use_compatible_constraint_for_large_tables__warning(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=1,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_field_with_not_null__use_compatible_constraint_for_large_tables__with_flexible_timeout__warning(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=False)
def test_add_field_with_not_null__use_compatible_constraint_for_all_tables__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=False,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_field_with_not_null__use_compatible_constraint_for_all_tables__with_flexible_timeout__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=True)
def test_add_field_with_not_null__allowed_for_all_tables__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=10)
def test_add_field_with_not_null__allowed_for_small_tables__raise(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=1)
def test_add_field_with_not_null__use_compatible_constraint_for_large_tables__raise(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=False)
def test_add_field_with_not_null__use_compatible_constraint_for_all_tables__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ADD COLUMN NOT NULL is unsafe operation'):
            field = models.CharField(max_length=40, null=False)
            field.set_attributes_from_name('field')
            editor.add_field(Model, field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field_with_foreign_key__ok():
    with cmp_schema_editor() as editor:
        field = models.ForeignKey(Model2, null=True, on_delete=models.CASCADE)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.collected_sql == timeouts(
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
        ) + timeouts(
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
        ) + [
            'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
        ] + [
            'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        ]
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL '
            'CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'REFERENCES "tests_model2"("id") DEFERRABLE INITIALLY DEFERRED; '
            'SET CONSTRAINTS "tests_model_field_id_0166400c_fk_tests_model2_id" IMMEDIATE;',
            'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        ]
    else:
        assert editor.collected_sql == timeouts(
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
        ) + [
            'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        ] + timeouts(
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
        ) + [
            'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
        ]
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
            'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED;',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_field_with_foreign_key__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        field = models.ForeignKey(Model2, null=True, on_delete=models.CASCADE)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.collected_sql == timeouts(
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
        ) + timeouts(
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
        ) + flexible_statement_timeout(
            'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
        ) + flexible_statement_timeout(
            'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        )
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL '
            'CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'REFERENCES "tests_model2"("id") DEFERRABLE INITIALLY DEFERRED; '
            'SET CONSTRAINTS "tests_model_field_id_0166400c_fk_tests_model2_id" IMMEDIATE;',
            'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        ]
    else:
        assert editor.collected_sql == timeouts(
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
        ) + flexible_statement_timeout(
            'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        ) + timeouts(
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
        ) + flexible_statement_timeout(
            'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
        )
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ADD COLUMN "field_id" integer NULL;',
            'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
            'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
            'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED;',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field_with_primary_key__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40, null=True, primary_key=True)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL;',
    ) + [
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_pk" ON "tests_model" ("field");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" '
        'PRIMARY KEY USING INDEX "tests_model_field_0a53d95f_pk";',
    ) + [
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL PRIMARY KEY;',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_field_with_primary_key__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40, null=True, primary_key=True)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL;',
    ) + flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_pk" ON "tests_model" ("field");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" '
        'PRIMARY KEY USING INDEX "tests_model_field_0a53d95f_pk";',
    ) + flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL PRIMARY KEY;',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_field_with_unique__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40, null=True, unique=True)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL;',
    ) + [
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_uniq" ON "tests_model" ("field");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" '
        'UNIQUE USING INDEX "tests_model_field_0a53d95f_uniq";',
    ) + [
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" '
        'ON "tests_model" ("field" varchar_pattern_ops);',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL UNIQUE;',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_field_with_unique__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40, null=True, unique=True)
        field.set_attributes_from_name('field')
        editor.add_field(Model, field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL;',
    ) + flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_uniq" ON "tests_model" ("field");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" '
        'UNIQUE USING INDEX "tests_model_field_0a53d95f_uniq";',
    ) + flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" '
        'ON "tests_model" ("field" varchar_pattern_ops);',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD COLUMN "field" varchar(40) NULL UNIQUE;',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


def test_alter_field_varchar40_to_varchar20__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.CharField(max_length=40)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=20)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(20);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(20) USING "field"::varchar(20);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_varchar40_to_varchar20_error():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.CharField(max_length=40)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=20)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(20);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(20) USING "field"::varchar(20);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_varchar40_to_varchar80__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=80)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(80);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE varchar(80) USING "field"::varchar(80);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_varchar40_to_text__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.TextField()
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE text USING "field"::text;',
    ]


def test_alter_field_decimal10_2_to_decimal5_2__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=5, decimal_places=2)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(5, 2);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(5, 2) USING "field"::numeric(5, 2);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_decimal10_2_to_decimal5_2__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=5, decimal_places=2)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(5, 2);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(5, 2) USING "field"::numeric(5, 2);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_decimal10_2_to_decimal20_2__ok():
    with cmp_schema_editor() as editor:
        old_field = models.DecimalField(max_digits=10, decimal_places=2)
        old_field.set_attributes_from_name('field')
        new_field = models.DecimalField(max_digits=20, decimal_places=2)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(20, 2);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(20, 2) USING "field"::numeric(20, 2);',
        ]


def test_alter_field_decimal10_2_to_decimal10_3__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=10, decimal_places=3)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 3);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 3) USING "field"::numeric(10, 3);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_decimal10_2_to_decimal10_3__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=10, decimal_places=3)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 3);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 3) USING "field"::numeric(10, 3);',
        ]


def test_alter_field_decimal10_2_to_decimal10_1__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=10, decimal_places=1)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 1);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 1) USING "field"::numeric(10, 1);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_decimal10_2_to_decimal10_1__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER COLUMN TYPE is unsafe operation'):
            old_field = models.DecimalField(max_digits=10, decimal_places=2)
            old_field.set_attributes_from_name('field')
            new_field = models.DecimalField(max_digits=10, decimal_places=1)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    if django.VERSION[:2] >= (3, 0):
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 1);',
        ]
    else:
        assert editor.django_sql == [
            'ALTER TABLE "tests_model" ALTER COLUMN "field" TYPE numeric(10, 1) USING "field"::numeric(10, 1);',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_set_not_null__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ] + timeouts(
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;'
    ) + timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_notnull";'
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_alter_field_set_not_null__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ) + timeouts(
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;'
    ) + timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_notnull";'
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL='USE_PG_ATTRIBUTE_UPDATE_FOR_SUPERUSER')
@old_pg
def test_alter_field_set_not_null__old_pg__use_pg_attribute_update__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ] + [
        'UPDATE pg_catalog.pg_attribute SET attnotnull = TRUE '
        'WHERE attrelid = \'"tests_model"\'::regclass::oid AND attname = replace(\'"field"\', \'"\', \'\');',
    ] + timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_notnull";'
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL='USE_PG_ATTRIBUTE_UPDATE_FOR_SUPERUSER',
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
@old_pg
def test_alter_field_set_not_null__old_pg__use_pg_attribute_update__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ) + [
        'UPDATE pg_catalog.pg_attribute SET attnotnull = TRUE '
        'WHERE attrelid = \'"tests_model"\'::regclass::oid AND attname = replace(\'"field"\', \'"\', \'\');',
    ] + timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_notnull";'
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@old_pg
def test_alter_field_set_not_null__old_pg__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN NOT NULL is unsafe operation'):
            old_field = models.CharField(max_length=40, null=True)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=40, null=False)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
@old_pg
def test_alter_field_set_not_null__old_pg__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER COLUMN NOT NULL is unsafe operation'):
            old_field = models.CharField(max_length=40, null=True)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=40, null=False)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=True)
@old_pg
def test_alter_field_set_not_null__old_pg__allowed_for_all_tables__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN NOT NULL is unsafe operation'):
            old_field = models.CharField(max_length=40, null=True)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=40, null=False)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=10)
@old_pg
def test_alter_field_set_not_null__old_pg__allowed_for_small_tables__warning(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER COLUMN NOT NULL is unsafe operation'):
            old_field = models.CharField(max_length=40, null=True)
            old_field.set_attributes_from_name('field')
            new_field = models.CharField(max_length=40, null=False)
            new_field.set_attributes_from_name('field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=1)
@old_pg
def test_alter_field_set_not_null__old_pg__use_compatible_constraint_for_large_tables__ok(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=1,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
@old_pg
def test_alter_field_set_not_null__old_pg__use_compatible_constraint_for_large_tables__with_flexible_timeout__ok(
    mocker
):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (5,)
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=False)
@old_pg
def test_alter_field_set_not_null__old_pg__use_compatible_constraint_for_all_tables__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_USE_NOT_NULL=False,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
@old_pg
def test_alter_field_set_not_null__old_pg__use_compatible_constraint_for_all_tables__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=False)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_notnull" '
        'CHECK ("field" IS NOT NULL) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_notnull";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" SET NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_filed_drop_not_null__ok(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = None
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=False)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" DROP NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_filed_drop_not_null_constraint__ok(mocker):
    mocker.patch.object(connection, 'cursor')().__enter__().fetchone.return_value = (
        'tests_model_field_0a53d95f_notnull',
    )
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, null=False)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, null=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT tests_model_field_0a53d95f_notnull;',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ALTER COLUMN "field" DROP NOT NULL;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_set_default__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, default='test')
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    # no sql executed because django doesn't use database defaults
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == []


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_drop_default__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, default='test')
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    # no sql executed because django doesn't use database defaults
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == []


def test_rename_field__warning():
    with cmp_schema_editor() as editor:
        with pytest.warns(UnsafeOperationWarning, match='ALTER TABLE RENAME COLUMN is unsafe operation'):
            old_field = models.CharField(max_length=40)
            old_field.set_attributes_from_name('old_field')
            new_field = models.CharField(max_length=40)
            new_field.set_attributes_from_name('new_field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" RENAME COLUMN "old_field" TO "new_field";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_rename_field__raise():
    with cmp_schema_editor() as editor:
        with pytest.raises(UnsafeOperationException, match='ALTER TABLE RENAME COLUMN is unsafe operation'):
            old_field = models.CharField(max_length=40)
            old_field.set_attributes_from_name('old_field')
            new_field = models.CharField(max_length=40)
            new_field.set_attributes_from_name('new_field')
            editor.alter_field(Model, old_field, new_field)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" RENAME COLUMN "old_field" TO "new_field";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_remove_field__ok():
    with cmp_schema_editor() as editor:
        field = models.CharField(max_length=40)
        field.set_attributes_from_name('field')
        editor.remove_field(Model, field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP COLUMN "field" CASCADE;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_add_constraint_check__ok():
    with cmp_schema_editor() as editor:
        old_field = models.IntegerField()
        old_field.set_attributes_from_name('field')
        new_field = models.PositiveIntegerField()
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_check" '
        'CHECK ("field" >= 0) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_check";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_check" CHECK ("field" >= 0);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_alter_field_add_constraint_check__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.IntegerField()
        old_field.set_attributes_from_name('field')
        new_field = models.PositiveIntegerField()
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_check" '
        'CHECK ("field" >= 0) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_0a53d95f_check";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_check" CHECK ("field" >= 0);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_drop_constraint_check__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_0a53d95f_check': {
            'columns': ['field'],
            'primary_key': False,
            'unique': False,
            'foreign_key': None,
            'check': True,
            'index': False,
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        old_field = models.PositiveIntegerField()
        old_field.set_attributes_from_name('field')
        new_field = models.IntegerField()
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_check";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_filed_add_constraint_foreign_key__ok():
    with cmp_schema_editor() as editor:
        old_field = models.IntegerField()
        old_field.set_attributes_from_name('field_id')
        new_field = models.ForeignKey(Model2, on_delete=models.CASCADE)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
        'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
        'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_alter_filed_add_constraint_foreign_key__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.IntegerField()
        old_field.set_attributes_from_name('field_id')
        new_field = models.ForeignKey(Model2, on_delete=models.CASCADE)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
        'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id";',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field_id_0166400c" ON "tests_model" ("field_id");',
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_id_0166400c_fk_tests_model2_id" '
        'FOREIGN KEY ("field_id") REFERENCES "tests_model2" ("id") DEFERRABLE INITIALLY DEFERRED;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_drop_constraint_foreign_key__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_0a53d95f_pk': {
            'columns': ['field_id'],
            'primary_key': False,
            'unique': False,
            'foreign_key': (Model2._meta.db_table, 'id'),
            'check': False,
            'index': False,
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        old_field = models.ForeignKey(Model2, on_delete=models.CASCADE)
        old_field.set_attributes_from_name('field')
        new_field = models.IntegerField()
        new_field.set_attributes_from_name('field_id')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'SET CONSTRAINTS "tests_model_field_0a53d95f_pk" IMMEDIATE; '
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_pk";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_add_constraint_primary_key__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, unique=True)
        old_field.set_attributes_from_name('field')
        old_field.model = Model
        new_field = models.CharField(max_length=40, primary_key=True)
        new_field.set_attributes_from_name('field')
        new_field.model = Model
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_pk" ON "tests_model" ("field");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" '
        'PRIMARY KEY USING INDEX "tests_model_field_0a53d95f_pk";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" PRIMARY KEY ("field");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_alter_field_add_constraint_primary_key__with_flexible_timeout__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, unique=True)
        old_field.set_attributes_from_name('field')
        old_field.model = Model
        new_field = models.CharField(max_length=40, primary_key=True)
        new_field.set_attributes_from_name('field')
        new_field.model = Model
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_pk" ON "tests_model" ("field");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" '
        'PRIMARY KEY USING INDEX "tests_model_field_0a53d95f_pk";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_pk" PRIMARY KEY ("field");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_drop_constraint_primary_key__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_0a53d95f_pk': {
            'columns': ['field'],
            'primary_key': True,
            'unique': True,
            'foreign_key': None,
            'check': False,
            'index': False,
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, primary_key=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_pk";',
    ) + [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field_0a53d95f_like";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_pk";',
        'DROP INDEX IF EXISTS "tests_model_field_0a53d95f_like";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_add_constraint_unique__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, unique=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_uniq" ON "tests_model" ("field");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" '
        'UNIQUE USING INDEX "tests_model_field_0a53d95f_uniq";',
    ) + [
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" '
        'ON "tests_model" ("field" varchar_pattern_ops);',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" UNIQUE ("field");',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_alter_field_add_constraint_unique__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, unique=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field_0a53d95f_uniq" ON "tests_model" ("field");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" '
        'UNIQUE USING INDEX "tests_model_field_0a53d95f_uniq";',
    ) + flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" '
        'ON "tests_model" ("field" varchar_pattern_ops);',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field_0a53d95f_uniq" UNIQUE ("field");',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_alter_field_drop_constraint_unique__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_0a53d95f_uniq': {
            'columns': ['field'],
            'primary_key': False,
            'unique': True,
            'foreign_key': None,
            'check': False,
            'index': False,
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, unique=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_uniq";',
    ) + [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field_0a53d95f_like";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_0a53d95f_uniq";',
        'DROP INDEX IF EXISTS "tests_model_field_0a53d95f_like";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_index__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, db_index=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f" ON "tests_model" ("field");',
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field_0a53d95f" ON "tests_model" ("field");',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40, db_index=True)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f" ON "tests_model" ("field");',
    ) + flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field_0a53d95f" ON "tests_model" ("field");',
        'CREATE INDEX "tests_model_field_0a53d95f_like" ON "tests_model" ("field" varchar_pattern_ops);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_remove_index__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_idx': {
            'columns': ['field'],
            'orders': ['ASC'],
            'primary_key': False,
            'unique': False,
            'foreign_key': None,
            'check': False,
            'index': True,
            'type': 'idx',
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        old_field = models.CharField(max_length=40, db_index=True)
        old_field.set_attributes_from_name('field')
        new_field = models.CharField(max_length=40)
        new_field.set_attributes_from_name('field')
        editor.alter_field(Model, old_field, new_field)
    assert editor.collected_sql == [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field_idx";',
    ]
    assert editor.django_sql == [
        'DROP INDEX IF EXISTS "tests_model_field_idx";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_unique_together__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        editor.alter_unique_together(Model, [], [['field1', 'field2']])
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field1_field2_51878e08_uniq" '
        'ON "tests_model" ("field1", "field2");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field1_field2_51878e08_uniq" '
        'UNIQUE USING INDEX "tests_model_field1_field2_51878e08_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field1_field2_51878e08_uniq" '
        'UNIQUE ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_unique_together__with_flexible_timeout__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        editor.alter_unique_together(Model, [], [['field1', 'field2']])
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "tests_model_field1_field2_51878e08_uniq" '
        'ON "tests_model" ("field1", "field2");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field1_field2_51878e08_uniq" '
        'UNIQUE USING INDEX "tests_model_field1_field2_51878e08_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "tests_model_field1_field2_51878e08_uniq" '
        'UNIQUE ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_remove_unique_together__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_idx': {
            'columns': ['field1', 'field2'],
            'primary_key': False,
            'unique': True,
            'foreign_key': None,
            'check': False,
            'index': False,
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        editor.alter_unique_together(Model, [['field1', 'field2']], [])
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "tests_model_field_idx";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_index_together__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        editor.alter_index_together(Model, [], [['field1', 'field2']])
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_field2_51878e08_idx" '
        'ON "tests_model" ("field1", "field2");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_field2_51878e08_idx" ON "tests_model" ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_index_together__with_flexible_timeout__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    with cmp_schema_editor() as editor:
        editor.alter_index_together(Model, [], [['field1', 'field2']])
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_field2_51878e08_idx" '
        'ON "tests_model" ("field1", "field2");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_field2_51878e08_idx" ON "tests_model" ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_remove_index_together__ok(mocker):
    mocker.patch.object(connection, 'cursor')
    mocker.patch.object(connection.introspection, 'get_constraints').return_value = {
        'tests_model_field_idx': {
            'columns': ['field1', 'field2'],
            'orders': ['ASC', 'ASC'],
            'primary_key': False,
            'unique': False,
            'foreign_key': None,
            'check': False,
            'index': True,
            'type': 'idx',
            'definition': None,
            'options': None,
        }
    }
    with cmp_schema_editor() as editor:
        editor.alter_index_together(Model, [['field1', 'field2']], [])
    assert editor.collected_sql == [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field_idx";',
    ]
    assert editor.django_sql == [
        'DROP INDEX IF EXISTS "tests_model_field_idx";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_check_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.CheckConstraint(check=models.Q(field1__gt=0), name='field1_gt_0'))
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_gt_0" '
        'CHECK ("field1" > 0) NOT VALID;',
    ) + [
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "field1_gt_0";',
    ]
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_gt_0" CHECK ("field1" > 0);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_check_constraint__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.CheckConstraint(check=models.Q(field1__gt=0), name='field1_gt_0'))
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_gt_0" '
        'CHECK ("field1" > 0) NOT VALID;',
    ) + flexible_statement_timeout(
        'ALTER TABLE "tests_model" VALIDATE CONSTRAINT "field1_gt_0";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_gt_0" CHECK ("field1" > 0);',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_meta_check_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.remove_constraint(Model, models.CheckConstraint(check=models.Q(field1__gt=0), name='field1_gt_0'))
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "field1_gt_0";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "field1_gt_0";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_unique_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.UniqueConstraint(fields=('field1',), name='field1_uniq'))
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "field1_uniq" ON "tests_model" ("field1");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_uniq" '
        'UNIQUE USING INDEX "field1_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_uniq" UNIQUE ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_unique_constraint__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.UniqueConstraint(fields=('field1',), name='field1_uniq'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE UNIQUE INDEX CONCURRENTLY "field1_uniq" ON "tests_model" ("field1");',
    ) + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_uniq" '
        'UNIQUE USING INDEX "field1_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_uniq" UNIQUE ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_multicolumn_unique_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.UniqueConstraint(fields=('field1', 'field2'), name='field1_field2_uniq'))
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "field1_field2_uniq" ON "tests_model" ("field1", "field2");',
    ] + timeouts(
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_field2_uniq" '
        'UNIQUE USING INDEX "field1_field2_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_field2_uniq" UNIQUE ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_conditional_unique_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.UniqueConstraint(
            fields=('field1',), name='field1_uniq', condition=models.Q(field1__gt=0)))
    assert editor.collected_sql == [
        'CREATE UNIQUE INDEX CONCURRENTLY "field1_uniq" ON "tests_model" ("field1") WHERE "field1" > 0;',
    ]
    assert editor.django_sql == [
        'CREATE UNIQUE INDEX "field1_uniq" ON "tests_model" ("field1") WHERE "field1" > 0;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_conditional_multicolumn_unique_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.add_constraint(Model, models.UniqueConstraint(
            fields=('field1', 'field2'), name='field1_field2_uniq', condition=models.Q(field1=models.F('field2'))))
    if django.VERSION[:2] >= (4, 0):
        assert editor.collected_sql == [
            'CREATE UNIQUE INDEX CONCURRENTLY "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = ("field2");',
        ]
        assert editor.django_sql == [
            'CREATE UNIQUE INDEX "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = ("field2");',
        ]
    elif django.VERSION[:2] >= (3, 0):
        assert editor.collected_sql == [
            'CREATE UNIQUE INDEX CONCURRENTLY "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = "field2";',
        ]
        assert editor.django_sql == [
            'CREATE UNIQUE INDEX "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = "field2";',
        ]
    else:
        assert editor.collected_sql == [
            'CREATE UNIQUE INDEX CONCURRENTLY "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = ("field2");',
        ]
        assert editor.django_sql == [
            'CREATE UNIQUE INDEX "field1_field2_uniq" ON "tests_model" ("field1", "field2") '
            'WHERE "field1" = ("field2");',
        ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_meta_unique_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.remove_constraint(Model, models.UniqueConstraint(fields=('field1',), name='field1_uniq'))
    assert editor.collected_sql == timeouts(
        'ALTER TABLE "tests_model" DROP CONSTRAINT "field1_uniq";',
    )
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "field1_uniq";',
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
def test_add_meta_exclusion_constraint__warning():
    with pytest.warns(UnsafeOperationWarning, match='ADD CONSTRAINT EXCLUDE is unsafe operation'):
        with cmp_schema_editor() as editor:
            editor.add_constraint(Model, ExclusionConstraint(expressions=[('field1', '=')], name='field1_excluded'))
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_excluded" EXCLUDE USING GIST ("field1" WITH =);',
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_exclusion_constraint__raise():
    with pytest.raises(UnsafeOperationException, match='ADD CONSTRAINT EXCLUDE is unsafe operation'):
        with cmp_schema_editor() as editor:
            editor.add_constraint(Model, ExclusionConstraint(expressions=[('field1', '=')], name='field1_excluded'))
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" ADD CONSTRAINT "field1_excluded" EXCLUDE USING GIST ("field1" WITH =);',
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_meta_exclusion_constraint__ok():
    with cmp_schema_editor() as editor:
        editor.remove_constraint(Model, ExclusionConstraint(expressions=[('field1', '=')], name='field1_excluded'))
    assert editor.collected_sql == timeouts(editor.django_sql)
    assert editor.django_sql == [
        'ALTER TABLE "tests_model" DROP CONSTRAINT "field1_excluded";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" '
        'ON "tests_model" ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" '
        'ON "tests_model" ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_multicolumn_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(fields=['field1', 'field2'], name='tests_model_field1_45bc7f_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_45bc7f_idx" '
        'ON "tests_model" ("field1", "field2");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_45bc7f_idx" ON "tests_model" ("field1", "field2");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_conditional_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(condition=models.Q(field1__gt=0), fields=['field1'], name='field1_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "field1_idx" ON "tests_model" ("field1") WHERE "field1" > 0;',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "field1_idx" ON "tests_model" ("field1") WHERE "field1" > 0;',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_conditional_multicolumn_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(condition=models.Q(field1__gt=0), fields=['field1', 'field2'],
                                             name='field1_field2_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "field1_field2_idx" ON "tests_model" ("field1", "field2") WHERE "field1" > 0;',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "field1_field2_idx" ON "tests_model" ("field1", "field2") WHERE "field1" > 0;',
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_index_concurrently__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'),
                         concurrently=True)
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" ("field1");'
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_index_concurrently__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'),
                         concurrently=True)
    assert editor.collected_sql == flexible_statement_timeout(editor.django_sql)
    assert editor.django_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" ("field1");'
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_meta_index__ok():
    with cmp_schema_editor() as editor:
        editor.remove_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field1_9b60dc_idx";',
    ]
    assert editor.django_sql == [
        'DROP INDEX IF EXISTS "tests_model_field1_9b60dc_idx";',
    ]


@pytest.mark.skipif(django.VERSION[:2] < (3, 0), reason='functionality provided in django 3.0')
@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_drop_meta_index_concurrently__ok():
    with cmp_schema_editor() as editor:
        editor.remove_index(Model, models.Index(fields=['field1'], name='tests_model_field1_9b60dc_idx'),
                            concurrently=True)
    assert editor.collected_sql == editor.django_sql
    assert editor.django_sql == [
        'DROP INDEX CONCURRENTLY IF EXISTS "tests_model_field1_9b60dc_idx";',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_brin_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, BrinIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING brin ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING brin ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_brin_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, BrinIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING brin ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING brin ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_btree_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, BTreeIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING btree ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING btree ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_btree_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, BTreeIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING btree ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING btree ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_gin_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, GinIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING gin ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING gin ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_gin_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, GinIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING gin ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING gin ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_gist_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, GistIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING gist ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING gist ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_gist_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, GistIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING gist ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING gist ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_hash_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, HashIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING hash ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING hash ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_hash_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, HashIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING hash ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING hash ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True)
def test_add_meta_spgist_index__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, SpGistIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == [
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING spgist ("field1");',
    ]
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING spgist ("field1");',
    ]


@override_settings(ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE=True,
                   ZERO_DOWNTIME_MIGRATIONS_FLEXIBLE_STATEMENT_TIMEOUT=True)
def test_add_meta_spgist_index__with_flexible_timeout__ok():
    with cmp_schema_editor() as editor:
        editor.add_index(Model, SpGistIndex(fields=['field1'], name='tests_model_field1_9b60dc_idx'))
    assert editor.collected_sql == flexible_statement_timeout(
        'CREATE INDEX CONCURRENTLY "tests_model_field1_9b60dc_idx" ON "tests_model" USING spgist ("field1");',
    )
    assert editor.django_sql == [
        'CREATE INDEX "tests_model_field1_9b60dc_idx" ON "tests_model" USING spgist ("field1");',
    ]
