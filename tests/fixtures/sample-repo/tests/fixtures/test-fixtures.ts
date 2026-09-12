import { test as base } from '@playwright/test';
import { LoginPage } from '../pages/LoginPage';
import { DashboardPage } from '../pages/DashboardPage';
import { getUser } from '../utils/test-users';

type Fixtures = {
  loginPage: LoginPage;
  dashboardPage: DashboardPage;
  authenticatedPage: DashboardPage;
};

/** Reuse these fixtures — do not create browser contexts by hand. */
export const test = base.extend<Fixtures>({
  loginPage: async ({ page }, use) => {
    await use(new LoginPage(page));
  },
  dashboardPage: async ({ page }, use) => {
    await use(new DashboardPage(page));
  },
  authenticatedPage: async ({ page }, use) => {
    const login = new LoginPage(page);
    const user = getUser('standard');
    await login.goto();
    await login.login(user.username, user.password);
    await use(new DashboardPage(page));
  },
});

export { expect } from '@playwright/test';
