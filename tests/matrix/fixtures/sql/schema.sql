CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    total REAL
);

CREATE VIEW user_orders AS
SELECT u.name, o.total
FROM users u
JOIN orders o ON o.user_id = u.id;
