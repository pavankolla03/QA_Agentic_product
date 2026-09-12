import { Given, When, Then } from '@cucumber/cucumber';
import { LoginPage } from '../pages/LoginPage';
import { DashboardPage } from '../pages/DashboardPage';
import { getUser } from '../utils/test-users';

let loginPage: LoginPage;
let dashboardPage: DashboardPage;

Given('I am on the login page', async function () {
  loginPage = new LoginPage(this.page);
  await loginPage.goto();
});

When('I sign in as a standard user', async function () {
  const user = getUser('standard');
  await loginPage.login(user.username, user.password);
});

When('I sign in with an incorrect password', async function () {
  const user = getUser('standard');
  await loginPage.login(user.username, 'definitely-not-the-password');
});

Then('I should see the dashboard', async function () {
  dashboardPage = new DashboardPage(this.page);
  await dashboardPage.expectLoaded();
});

Then('I should see the message {string}', async function (message: string) {
  await loginPage.expectLoginError(message);
});
