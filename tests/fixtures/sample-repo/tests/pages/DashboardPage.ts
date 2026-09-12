import { expect } from '@playwright/test';
import { BasePage } from './BasePage';

export class DashboardPage extends BasePage {
  readonly path = '/dashboard';

  async expectLoaded(): Promise<void> {
    await expect(this.page.getByTestId('dashboard-heading')).toBeVisible();
  }

  async openSection(name: string): Promise<void> {
    await this.page.getByRole('link', { name }).click();
  }
}
