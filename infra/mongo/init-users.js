const databases = ["auth", "clients", "inventory", "orders", "payments"];
for (const name of databases) {
  const password = process.env["DB_PASS_" + name.toUpperCase()];
  db.getSiblingDB(name).createUser({
    user: name + "_svc",
    pwd: password,
    roles: [{ role: "readWrite", db: name }],
  });
}
