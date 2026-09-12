@auth @regression
Feature: User login
  As a registered user I want to sign in so that I can access my dashboard.

  Background:
    Given I am on the login page

  @smoke @P1
  Scenario: TC-AUTH-001 Sign in with valid credentials
    When I sign in as a standard user
    Then I should see the dashboard

  @P2 @negative
  Scenario: TC-AUTH-002 Reject invalid credentials
    When I sign in with an incorrect password
    Then I should see the message "Invalid username or password"
