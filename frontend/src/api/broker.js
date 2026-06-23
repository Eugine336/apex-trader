import client from "./client";

// GET /api/broker → list[BrokerCredentialResponse]
export async function listCredentials() {
  const { data } = await client.get("/api/broker");
  return data;
}

// PUT /api/broker/mt5 → MessageResponse
// payload: { login: number, password, server, label? }
export async function setMT5({ login, password, server, label = "" }) {
  const { data } = await client.put("/api/broker/mt5", {
    broker_type: "mt5",
    login: Number(login),
    password,
    server,
    label,
  });
  return data;
}

// PUT /api/broker/deriv → MessageResponse
// payload: { access_token, app_id, account_type, client_id?, label? }
export async function setDeriv({
  access_token,
  app_id,
  account_type = "demo",
  client_id = "",
  label = "",
}) {
  const { data } = await client.put("/api/broker/deriv", {
    broker_type: "deriv",
    access_token,
    app_id,
    account_type,
    client_id,
    label,
  });
  return data;
}

// DELETE /api/broker/{broker_type} → MessageResponse
export async function deleteCredentials(brokerType) {
  const { data } = await client.delete(`/api/broker/${brokerType}`);
  return data;
}
