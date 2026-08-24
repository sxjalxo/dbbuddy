-- The sample business database the demo asks questions against.
--
-- Runs once, against POSTGRES_DB (dbbuddy_demo), on first boot of the volume.
--
-- This is a *demo dataset*, not engine knowledge. DB Buddy carries no hardcoded
-- table or column names — it reads this schema at analyze time like any other.
-- The shape is chosen to exercise the parts of the planner worth showing:
--
--   * declared foreign keys, so joins are inferred rather than guessed
--   * two- and three-hop joins (payments -> orders -> customers -> regions)
--   * a column name that appears on two tables (unit_price), so ambiguity
--     resolution and the confidence signal have something to do
--   * two tables referencing the same dimension (customers and employees both
--     point at regions)
--   * money, quantity, and date columns, so measures and temporal filters are
--     real rather than implied
--   * dates anchored to CURRENT_DATE, so "last month" keeps working forever
--
-- Deterministic: setseed() fixes the pseudo-random stream, so every fresh
-- volume produces byte-identical data and a demo answer is reproducible.

BEGIN;

CREATE TABLE regions (
    region_id   integer PRIMARY KEY,
    region_name varchar(60)  NOT NULL,
    country     varchar(60)  NOT NULL
);

CREATE TABLE customers (
    customer_id   integer PRIMARY KEY,
    customer_name varchar(120) NOT NULL,
    email         varchar(160) NOT NULL,
    region_id     integer      NOT NULL REFERENCES regions (region_id),
    segment       varchar(20)  NOT NULL,
    signup_date   date         NOT NULL
);

CREATE TABLE products (
    product_id   integer PRIMARY KEY,
    product_name varchar(120)  NOT NULL,
    category     varchar(40)   NOT NULL,
    unit_price   numeric(10,2) NOT NULL
);

CREATE TABLE employees (
    employee_id integer PRIMARY KEY,
    full_name   varchar(120) NOT NULL,
    title       varchar(60)  NOT NULL,
    region_id   integer      NOT NULL REFERENCES regions (region_id),
    hire_date   date         NOT NULL
);

CREATE TABLE orders (
    order_id    integer PRIMARY KEY,
    customer_id integer     NOT NULL REFERENCES customers (customer_id),
    employee_id integer     NOT NULL REFERENCES employees (employee_id),
    order_date  date        NOT NULL,
    status      varchar(20) NOT NULL
);

CREATE TABLE order_items (
    order_item_id integer PRIMARY KEY,
    order_id      integer       NOT NULL REFERENCES orders (order_id),
    product_id    integer       NOT NULL REFERENCES products (product_id),
    quantity      integer       NOT NULL,
    -- Deliberately shares a name with products.unit_price: the price *at the
    -- time of sale*. A question about "unit price" is genuinely ambiguous here,
    -- which is exactly what the confidence signal is for.
    unit_price    numeric(10,2) NOT NULL,
    discount      numeric(4,2)  NOT NULL DEFAULT 0
);

CREATE TABLE payments (
    payment_id integer PRIMARY KEY,
    order_id   integer       NOT NULL REFERENCES orders (order_id),
    paid_at    timestamp     NOT NULL,
    amount     numeric(12,2) NOT NULL,
    method     varchar(20)   NOT NULL
);

-- ── Data ────────────────────────────────────────────────────────────────────

SELECT setseed(0.42);

INSERT INTO regions (region_id, region_name, country) VALUES
    (1, 'North',     'United States'),
    (2, 'South',     'United States'),
    (3, 'West',      'United States'),
    (4, 'Midlands',  'United Kingdom'),
    (5, 'Bavaria',   'Germany'),
    (6, 'Kanto',     'Japan'),
    (7, 'Maharashtra', 'India'),
    (8, 'New South Wales', 'Australia');

INSERT INTO products (product_id, product_name, category, unit_price)
SELECT
    g,
    (ARRAY['Hydraulic Pump','Bearing Assembly','Control Module','Drive Belt',
           'Pressure Valve','Sensor Array','Coupling Kit','Gearbox Housing',
           'Filter Cartridge','Thermal Probe'])[1 + (g % 10)] || ' ' ||
        (ARRAY['MK1','MK2','MK3','XL','Pro'])[1 + (g % 5)],
    (ARRAY['Hydraulics','Electronics','Drivetrain','Instrumentation','Consumables'])[1 + (g % 5)],
    ROUND((25 + random() * 900)::numeric, 2)
FROM generate_series(1, 60) AS g;

INSERT INTO employees (employee_id, full_name, title, region_id, hire_date)
SELECT
    g,
    (ARRAY['Ana','Bo','Chidi','Dara','Emeka','Farah','Gita','Hugo','Ines','Jonas',
           'Kira','Luca','Mira','Nadia','Omar','Priya','Quinn','Rosa','Sami','Tomas'])[1 + (g % 20)]
        || ' ' ||
    (ARRAY['Alvarez','Bennett','Cho','Dubois','Ekstrom','Fischer','Gupta','Haddad',
           'Ivanov','Jensen'])[1 + (g % 10)],
    (ARRAY['Account Executive','Senior Account Executive','Regional Manager','Sales Associate'])[1 + (g % 4)],
    1 + (g % 8),
    (CURRENT_DATE - ((400 + g * 23) % 2200) * INTERVAL '1 day')::date
FROM generate_series(1, 24) AS g;

INSERT INTO customers (customer_id, customer_name, email, region_id, segment, signup_date)
SELECT
    g,
    (ARRAY['Northwind','Acme','Contoso','Globex','Initech','Umbrella','Vertex',
           'Cyberdyne','Soylent','Tyrell','Wayne','Stark'])[1 + (g % 12)]
        || ' ' ||
    (ARRAY['Industrial','Logistics','Manufacturing','Systems','Holdings','Works'])[1 + (g % 6)],
    'contact' || g || '@example.com',
    1 + (g % 8),
    (ARRAY['Enterprise','Mid-Market','SMB'])[1 + (g % 3)],
    (CURRENT_DATE - ((90 + g * 7) % 1400) * INTERVAL '1 day')::date
FROM generate_series(1, 400) AS g;

-- Orders spread across the last two years, weighted toward recent months so
-- "last month" and "this quarter" return a meaningful number of rows.
INSERT INTO orders (order_id, customer_id, employee_id, order_date, status)
SELECT
    g,
    1 + (g * 7 % 400),
    1 + (g % 24),
    (CURRENT_DATE - (FLOOR(random() * random() * 730))::int * INTERVAL '1 day')::date,
    (ARRAY['completed','completed','completed','completed',
           'pending','cancelled','refunded'])[1 + (g % 7)]
FROM generate_series(1, 5000) AS g;

INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price, discount)
SELECT
    ROW_NUMBER() OVER (),
    o.order_id,
    p.product_id,
    1 + (o.order_id + p.product_id) % 12,
    -- The cast is load-bearing: random() is double precision, so the product is
    -- double precision, and round(double precision, integer) does not exist in
    -- PostgreSQL. Without ::numeric this whole file aborts on first boot.
    ROUND((p.unit_price * (0.92 + random() * 0.16))::numeric, 2),
    ROUND((ARRAY[0, 0, 0, 0.05, 0.10, 0.15])[1 + ((o.order_id + p.product_id) % 6)]::numeric, 2)
FROM orders o
CROSS JOIN LATERAL (
    SELECT product_id, unit_price
    FROM products
    ORDER BY (product_id * 31 + o.order_id * 17) % 60
    LIMIT 1 + (o.order_id % 4)
) AS p;

-- Only completed and refunded orders were ever paid: a COUNT over payments and
-- a COUNT over orders legitimately differ, which is the kind of thing a naive
-- generator flattens and a real question trips over.
INSERT INTO payments (payment_id, order_id, paid_at, amount, method)
SELECT
    ROW_NUMBER() OVER (ORDER BY o.order_id),
    o.order_id,
    o.order_date + ((o.order_id % 5) * INTERVAL '1 day') + INTERVAL '11 hours',
    t.total,
    (ARRAY['card','bank_transfer','invoice','direct_debit'])[1 + (o.order_id % 4)]
FROM orders o
JOIN (
    SELECT order_id, ROUND(SUM(quantity * unit_price * (1 - discount)), 2) AS total
    FROM order_items
    GROUP BY order_id
) AS t ON t.order_id = o.order_id
WHERE o.status IN ('completed', 'refunded');

CREATE INDEX ON orders (customer_id);
CREATE INDEX ON orders (order_date);
CREATE INDEX ON order_items (order_id);
CREATE INDEX ON payments (order_id);

COMMIT;

ANALYZE;
