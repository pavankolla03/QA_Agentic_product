import { Page, expect } from '@playwright/test';

/** Shared behaviour for every Page Object. Extend this, never duplicate it. */
export abstract class BasePage {
  constructor(protected readonly page: Page) {}

  abstract readonly path: string;

  async goto(): Promise<void> {
    await this.page.goto(this.path);
    await this.waitUntilReady();
  }

  async waitUntilReady(): Promise<void> {
    await this.page.waitForLoadState('domcontentloaded');
  }

  async expectToast(message: string): Promise<void> {
    await expect(this.page.getByTestId('toast')).toContainText(message);
  }

  protected async fillIfPresent(testId: string, value?: string): Promise<void> {
    if (!value) return;
    await this.page.getByTestId(testId).fill(value);
  }
}
