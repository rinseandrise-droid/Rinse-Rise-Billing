/** API client — all billing data stored in SQLite via Flask backend. */

const API = {
  async request(path, options = {}, { retries = 0 } = {}) {
    let lastError = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const res = await fetch(path, {
          headers: { "Content-Type": "application/json", ...options.headers },
          ...options,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          let msg = err.error || `Request failed (${res.status})`;
          if (res.status === 503 && err.code === "database_unavailable") {
            msg =
              err.error ||
              "Database is not connected on the hosted server. Fix DATABASE_URL in Railway Variables and redeploy.";
          }
          if (res.status === 500 && /deadlock detected/i.test(msg)) {
            msg =
              "Database was busy for a moment. Click Save Bill again — check History in case the bill was already saved.";
          }
          if (res.status === 404 && path.includes("/send-whatsapp")) {
            throw new Error(
              "WhatsApp send API not found. Please restart Start Billing.bat and refresh the page (Ctrl+F5)."
            );
          }
          if (res.status === 405) {
            throw new Error(
              "Server needs a restart. Close Start Billing.bat, run it again, then press Ctrl+F5."
            );
          }
          throw new Error(msg);
        }
        return await res.json();
      } catch (err) {
        lastError = err;
        const isNetwork =
          err instanceof TypeError ||
          /failed to fetch|networkerror|load failed/i.test(String(err?.message || err));
        if (isNetwork && attempt < retries) {
          await new Promise((resolve) => setTimeout(resolve, 800 * (attempt + 1)));
          continue;
        }
        if (isNetwork) {
          throw new Error(
            "Could not reach the billing server. Check your internet, wait a few seconds, then try Save Bill again. If it still fails, refresh the page (Ctrl+Shift+R)."
          );
        }
        throw err;
      }
    }
    throw lastError || new Error("Request failed.");
  },

  health() {
    return this.request("/api/health");
  },

  live() {
    return this.request("/api/live");
  },

  getBillCounter() {
    return this.request("/api/settings/bill-counter");
  },

  getBills() {
    return this.request("/api/bills");
  },

  createBill(bill) {
    return this.createBillWithUniqueNumber(bill);
  },

  async createBillWithUniqueNumber(bill) {
    try {
      return await this.request(
        "/api/bills",
        {
          method: "POST",
          body: JSON.stringify(bill),
        },
        { retries: 2 }
      );
    } catch (err) {
      const msg = String(err?.message || err);
      if (!/already exists|unique constraint|bills_bill_no/i.test(msg)) {
        throw err;
      }
      const nextNo = await this.nextFreeBillNo();
      return await this.request(
        "/api/bills",
        {
          method: "POST",
          body: JSON.stringify({ ...bill, billNo: nextNo }),
        },
        { retries: 1 }
      );
    }
  },

  async nextFreeBillNo() {
    const bills = await this.getBills();
    const used = new Set((bills || []).map((b) => String(b.billNo || "").trim()));
    let n = 1;
    while (n < 1000) {
      const padded = String(n).padStart(4, "0");
      if (!used.has(padded) && !used.has(String(n))) return padded;
      n += 1;
    }
    return String(n);
  },

  updateBillStatus(billId, deliveryStatus) {
    return this.request(`/api/bills/${billId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ deliveryStatus }),
    });
  },

  updateBill(billId, bill) {
    return this.request(`/api/bills/${billId}`, {
      method: "PUT",
      body: JSON.stringify(bill),
    });
  },

  getCustomerProfile(phoneKey) {
    return this.request(`/api/customers/${encodeURIComponent(phoneKey)}/profile`);
  },

  getCustomerFavorite(phoneKey) {
    return this.request(`/api/customers/${encodeURIComponent(phoneKey)}/favorite`);
  },

  setCustomerFavorite(phoneKey, isFavorite, phone = "", name = "") {
    return this.request(`/api/customers/${encodeURIComponent(phoneKey)}/favorite`, {
      method: "PATCH",
      body: JSON.stringify({ isFavorite, phone, name }),
    });
  },

  migrate(data) {
    return this.request("/api/migrate", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  getExpenditures(fromDate = "", toDate = "") {
    const params = new URLSearchParams();
    if (fromDate) params.set("from", fromDate);
    if (toDate) params.set("to", toDate);
    const qs = params.toString();
    return this.request(`/api/expenditures${qs ? `?${qs}` : ""}`);
  },

  createExpenditure(name, amount, date = "") {
    return this.request("/api/expenditures", {
      method: "POST",
      body: JSON.stringify({ name, amount, date }),
    });
  },

  deleteExpenditure(id) {
    return this.request(`/api/expenditures/${id}`, { method: "DELETE" });
  },

  getProfitLoss(fromDate = "", toDate = "") {
    const params = new URLSearchParams();
    if (fromDate) params.set("from", fromDate);
    if (toDate) params.set("to", toDate);
    const qs = params.toString();
    return this.request(`/api/reports/profit-loss${qs ? `?${qs}` : ""}`);
  },

  getOverallStats() {
    return this.request("/api/reports/overall");
  },

  getOffers() {
    return this.request("/api/offers");
  },

  getRates() {
    return this.request("/api/rates");
  },

  saveRates(rates, password) {
    return this.request("/api/rates", {
      method: "PUT",
      body: JSON.stringify({ ...rates, password }),
    });
  },

  saveOffers(offers, password) {
    return this.request("/api/offers", {
      method: "PUT",
      body: JSON.stringify({ offers, password }),
    });
  },

  getWhatsAppStatus(startBridge = false) {
    const qs = startBridge ? "?start=1" : "";
    return this.request(`/api/whatsapp/status${qs}`);
  },

  startWhatsAppBridge() {
    return this.request("/api/whatsapp/start", {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  resetWhatsAppSession() {
    return this.request("/api/whatsapp/reset", {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  sendBillWhatsApp(billId, { skipPaymentValidation = false } = {}) {
    return this.request(`/api/bills/${billId}/send-whatsapp`, {
      method: "POST",
      body: JSON.stringify({ skipPaymentValidation }),
    });
  },

  getUniqueCustomerPhones() {
    return this.request("/api/customers/unique-phones");
  },

  async startOfferBroadcast(formData) {
    const res = await fetch("/api/offers/broadcast", {
      method: "POST",
      body: formData,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Broadcast failed (${res.status})`);
    return data;
  },

  getOfferBroadcastStatus(jobId) {
    return this.request(`/api/offers/broadcast/${encodeURIComponent(jobId)}`);
  },

  async fetchBillInvoicePdf(billId) {
    const res = await fetch(`/api/bills/${billId}/invoice.pdf`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `Could not fetch invoice PDF (${res.status})`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || `RinseRise-Invoice-${billId}.pdf`;
    return { blob, filename };
  },
};
