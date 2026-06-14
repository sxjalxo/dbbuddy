# Test prompts


# 🧪 1. 🟢 Basic Sanity (Should ALWAYS Work)

### Prompt
List all users with their emails

### Expected SQL
SELECT name, email FROM users;

### Generated SQL
SELECT name, email FROM users;


### What was tested
* Basic column mapping
* Table detection

---

### Prompt
Show all products and their prices

### Expected SQL
SELECT name, price FROM products;

### Generated SQL
SELECT name, price FROM products;

---

# 🧪 2. 🟡 Semantic Mapping (VERY IMPORTANT)

### Prompt
Show total revenue

### Expected SQL
SELECT SUM(total_amount) FROM orders;

### Generated SQL
SELECT SUM(total_amount) AS total_revenue FROM orders;

### What was tested

* revenue → total_amount mapping
* aggregation inference

---

### Prompt
Show users from India

### Expected SQL
SELECT * FROM users WHERE country = 'India';

### Generated SQL
SELECT * FROM users WHERE users.country = 'India';

---

# 🧪 3. 🔵 Join Intelligence (CORE DIFFERENTIATOR)

### Prompt
List users and their order amounts

### Expected SQL
SELECT u.name, o.total_amount
FROM users u
JOIN orders o ON u.id = o.user_id;

### Generated SQL
SELECT orders.total_amount, users.name FROM orders JOIN users ON orders.user_id = users.id;

---

### Prompt
Show users with their order status

### Expected SQL
SELECT u.name, o.status
FROM users u
JOIN orders o ON u.id = o.user_id;

### Generated SQL
SELECT orders.total_amount, users.name FROM orders JOIN users ON orders.user_id = users.id;

---

### Prompt (multi-table)
List users and the products they purchased

### Expected SQL
SELECT u.name, p.name
FROM users u
JOIN orders o ON u.id = o.user_id
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id;

### Generated SQL
SELECT products.name, products.price, order_items.price, orders.total_amount, users.name FROM products JOIN order_items ON order_items.product_id = products.id JOIN orders ON order_items.order_id = orders.id JOIN users ON orders.user_id = users.id;

👉 This tested:

* relationship graph
* multi-hop joins

---

# 🧪 4. 🟣 Aggregation + GROUP BY (CRITICAL TEST)

### Prompt
Show total order amount per user

### Expected SQL
SELECT u.name, SUM(o.total_amount)
FROM users u
JOIN orders o ON u.id = o.user_id
GROUP BY u.id;

### Generated SQL
SELECT users.name, SUM(orders.total_amount) AS total_revenue FROM orders JOIN users ON orders.user_id = users.id GROUP BY users.id, users.name;

---

### Prompt (edge case)
Show number of orders per user

### Expected SQL
SELECT u.name, COUNT(o.id)
FROM users u
JOIN orders o ON u.id = o.user_id
GROUP BY u.id;

### Generated SQL
SELECT users.name, COUNT(orders.id) AS total_count FROM orders JOIN users ON orders.user_id = users.id GROUP BY users.id, users.name;

👉 This tests:

* aggregation validator
* GROUP BY correctness

---

# 🧪 5. 🟠 Time-Based Queries

### Prompt
Show orders from last month

### Expected SQL
SELECT *
FROM orders
WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH);

### Generated SQL
SELECT * FROM orders WHERE orders.created_at >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH);

---

### Prompt
Show total revenue last month

### Generated SQL
SELECT SUM(total_amount) AS total_revenue FROM orders WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH);

---

# 🧪 6. 🔴 Behavioral Analytics (ADVANCED / WOW FACTOR)

### Prompt
Show login events per user

### Expected SQL
SELECT u.name, COUNT(e.id)
FROM users u
JOIN events e ON u.id = e.user_id
WHERE e.event_type = 'login'
GROUP BY u.id;

### Generated SQL
SELECT users.name, COUNT(events.id) AS total_count FROM events JOIN users ON events.user_id = users.id WHERE events.event_type = 'login' GROUP BY users.id, users.name;

---

### Prompt
Show most used device by users

### Expected SQL
SELECT device, COUNT(*) 
FROM events
GROUP BY device;

### Generated SQL
 Query failed
Execution error
1054 (42S22): Unknown column 'events.device' in 'field list'

SELECT events.device, COUNT(*) AS total_count FROM users GROUP BY events.device ORDER BY total_count DESC LIMIT 10;
---

# 🧪 7. 🛡️ Safety Tests (VERY IMPORTANT FOR DEMO)

### Prompt
Delete all users

### Expected behavior

* ❌ NOT executed
* ⚠️ Warning shown
* 🔍 Dry run: ~3 rows

### Generated SQL
 Query failed
Execution error
1451 (23000): Cannot delete or update a parent row: a foreign key constraint fails (`dbbuddy_demo`.`events`, CONSTRAINT `events_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`))

Generated SQL
DELETE FROM users;

Run: Runs the query
Cancel: Cancels the query

Asks for confirmation, buttons are working as intended but query generated is wrong and 

---

### Prompt
Update all users set country = 'USA'

### Expected behavior

* requires confirmation
* dry run count = 3
* affected column = country

### Actual Behavior
UPDATE users SET country = 'usa';

Asks for confirmation, buttons are working as intended, generated query is correct but when you hit Run button, it starts loading again then returns this
 Query failed
Execution error
1205 (HY000): Lock wait timeout exceeded; try restarting transaction
Generated SQL

UPDATE users SET country = 'usa';

---

# 🧪 8. ❌ Relevance Detection (YOUR NEW FEATURE)

### Prompt
How are you?

### Expected
{
  "error": "This doesn't appear to be a database query"
}

### Generated
 Query failed
Execution error
This doesn't appear to be a database query.

---

### Prompt
Tell me a joke about SQL

### Expected
👉 Should be rejected

### Generated
 Query failed
Execution error
This doesn't appear to be a database query.


---

### Prompt (edge case)
users random nonsense blah blah

### Expected
👉 Should fail due to **low coverage**

### Generated SQL
 Query failed
Execution error
This doesn't appear to be a database query.

---

# 🧪 9. ⚠️ Silent Failure Detection

### Prompt
Show users with orders greater than 10000

👉 Expected:
* 0 rows
* low confidence
* warning about possible mismatch

👉 Generated:
* 0 rows
* high confidence
* no warning about mismatch
SELECT orders.total_amount, users.name FROM orders JOIN users ON orders.user_id = users.id WHERE orders.total_amount > 10000;

---

# 🧪 10. 🧠 Interpretation Transparency

### Prompt
Show revenue per user

### Expected UI
revenue → orders.total_amount

### Generated
SELECT users.name, SUM(orders.total_amount) AS total_revenue FROM users JOIN orders ON users.id = orders.user_id GROUP BY users.id, users.name;

---

# 🔥 Demo Flow (Use This in Presentation)

1. Basic → users/emails
2. Semantic → revenue
3. Join → users + orders
4. Aggregation → revenue per user
5. Advanced → products purchased
6. Safety → DELETE query
7. Intelligence → irrelevant query
