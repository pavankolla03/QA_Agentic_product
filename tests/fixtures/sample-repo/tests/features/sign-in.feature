@authentication
Feature: Sign in
  /dashboard has a form containing a password field (Password) alongside Username

  @P0 @smoke
  Scenario: TC-AUTO-001 Sign in with valid credentials
    Given I am on the Sign in page
    When I sign in on the Sign in page with valid credentials
    Then I am taken away from the Sign in page

  @P1 @negative @regression
  Scenario: TC-AUTO-002 Reject an incorrect password
    Given I am on the Sign in page
    When I sign in on the Sign in page with an incorrect password
    Then I am still on the Sign in page

  @P1 @negative @regression
  Scenario: TC-AUTO-003 Reject a submission with Username left empty
    Given I am on the Sign in page
    When I enter "QA autopilot" in the Password field on the Sign in page
    And I submit the Sign in form
    Then I am still on the Sign in page

  @P1 @negative @regression
  Scenario: TC-AUTO-004 Reject a submission with Password left empty
    Given I am on the Sign in page
    When I enter "QA Autopilot" in the Username field on the Sign in page
    And I submit the Sign in form
    Then I am still on the Sign in page
