import { Page, expect } from '@playwright/test';
import { BasePage } from './BasePage';

export class LoginPage extends BasePage {
  readonly path = '/login';

  constructor(page: Page) {
    super(page);
  }

  private get username() {
    return this.page.getByTestId('login-username');
  }

  private get password() {
    return this.page.getByTestId('login-password');
  }

  private get submit() {
    return this.page.getByRole('button', { name: 'Sign in' });
  }

  async login(username: string, password: string): Promise<void> {
    await this.username.fill(username);
    await this.password.fill(password);
    await this.submit.click();
  }

  async expectLoginError(message: string): Promise<void> {
    await expect(this.page.getByTestId('login-error')).toHaveText(message);
  }
}
