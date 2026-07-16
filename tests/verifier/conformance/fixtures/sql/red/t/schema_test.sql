BEGIN;
SELECT plan(1);
SELECT ok(false, 'deliberately failing check');
SELECT * FROM finish();
ROLLBACK;
