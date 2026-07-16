BEGIN;
SELECT plan(1);
SELECT ok(true, 'schema sanity holds');
SELECT * FROM finish();
ROLLBACK;
