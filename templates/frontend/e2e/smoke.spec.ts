import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

// Change the route and expectations to the product. Do not delete meaningful
// assertions merely to obtain green output. This is NOT an auth/tenant test suite.
test("public entry page responds and renders a main landmark", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const response = await page.goto("/");
  expect(response, "Expected a document response").not.toBeNull();
  expect(response!.status()).toBeLessThan(400);
  await expect(page.getByRole("main")).toBeVisible();
  expect(errors, "Unhandled browser errors").toEqual([]);
});

test("public entry page has no serious/critical automated accessibility violations", async ({ page }) => {
  await page.goto("/");
  const scan = await new AxeBuilder({ page }).analyze();
  const blocking = scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(blocking).toEqual([]);
});
