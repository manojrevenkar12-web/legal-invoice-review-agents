import { expect, type Page, test } from "@playwright/test";

async function signIn(page: Page, who: RegExp) {
  await page.goto("/");
  await page.getByRole("button", { name: who }).click();
}

test("a reviewer decides an open finding and the ledger settles it", async ({ page }) => {
  await signIn(page, /Reviewer, Kestrel/);
  await expect(page.getByRole("heading", { name: /Waiting for you/ })).toBeVisible();
  await page.locator(".queue-item").first().click();
  const openRow = page.locator(".ledger-row.is-open").first();
  await openRow.click();
  const accept = page.locator(".finding.status-proposed").first().getByRole("button", { name: /^(Accept|Confirm)/ });
  await accept.click();
  await expect(page.locator(".finding.status-accepted").first()).toContainText("Accepted by Reviewer (kestrel)");
});

test("one client's reviewer cannot open another client's invoice", async ({ page, request }) => {
  const list = await request.get("http://127.0.0.1:8080/v1/invoices", { headers: { authorization: "Bearer demo-reviewer-kestrel" } });
  const number = (await list.json())[0].number as string;
  await signIn(page, /Reviewer, Brightwater/);
  await page.goto(`/review/${encodeURIComponent(number)}`);
  await expect(page.getByRole("alert")).toContainText("not found");
  const direct = await request.get(`http://127.0.0.1:8080/v1/invoices/${number}`, { headers: { authorization: "Bearer demo-reviewer-brightwater" } });
  expect(direct.status()).toBe(404);
});

test("legal operations sees what changed between guideline versions and the sentence behind each rule", async ({ page }) => {
  await signIn(page, /Legal operations, Kestrel/);
  await expect(page.getByRole("heading", { name: "Billing guidelines" })).toBeVisible();
  await page.getByRole("button", { name: /Version 2/ }).click();
  await expect(page.locator(".changes")).toContainText("Travel time is not billable");
  await expect(page.locator(".rules")).toContainText("Research above 4 h a month is not paid");
  await page.getByLabel("Question").fill("Can we bill for travel time to a deposition?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.locator(".ask-answer")).toBeVisible();
});

test("a law firm appeals an adjustment and the reviewer sees it with a suggestion", async ({ page }) => {
  await signIn(page, /Okafor Brandt LLP for Kestrel/);
  await page.locator(".firm-item").first().click();
  const appealable = page.getByRole("button", { name: "Appeal this adjustment" }).first();
  await expect(appealable).toBeVisible();
  await appealable.click();
  await page.locator(".firm-appeal textarea").fill("This was a single task. The review and the analysis were one continuous piece of work.");
  await page.getByRole("button", { name: "Send appeal" }).click();
  await expect(page.locator(".firm-filed").first()).toContainText("Appeal received");
});

test("the evaluation page shows money, findings and drift", async ({ page }) => {
  await signIn(page, /Reviewer, Corvane/);
  await page.getByRole("link", { name: "Evaluation" }).click();
  await expect(page.getByRole("heading", { name: "Judgement findings" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Drift by firm and month" })).toContainText("lindqvist");
});

test("on a phone the console fits the screen", async ({ browser }) => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await signIn(page, /Reviewer, Kestrel/);
  await page.locator(".queue-item").first().click();
  await expect(page.locator(".ledger-row").nth(1)).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.close();
});
