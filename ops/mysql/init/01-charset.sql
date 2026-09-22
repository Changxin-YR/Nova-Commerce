-- Runs once, on first container initialisation only.
-- Guarantees utf8mb4 at the server and schema level (spec section 19).
ALTER DATABASE nova CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;