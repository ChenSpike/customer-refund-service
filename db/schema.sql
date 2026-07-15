CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id           TEXT PRIMARY KEY,
    customer_id        TEXT NOT NULL REFERENCES customers(customer_id),
    product_type       TEXT NOT NULL,
    purchase_date      TEXT NOT NULL,
    item_status        TEXT NOT NULL CHECK(item_status IN ('delivered','damaged','returned','unknown')),
    amount_paid        REAL NOT NULL,
    prior_refund_total REAL NOT NULL DEFAULT 0.0
);
