# TODO Project Timeline & Management Document

## Executive Summary

This document provides a comprehensive overview of all TODO items identified across the LinkedIn Engagement Manager codebase. A total of **77 TODO comments** have been cataloged, analyzed, and organized into 8 themed issue groups based on priority and business impact.

**CRITICAL REQUIREMENT**: Before implementing any TODO items, a comprehensive test infrastructure must be established. All TODO fixes MUST follow a Test-Driven Development (TDD) approach to prevent regressions and ensure code quality.

### Priority Distribution
- **P0 (Critical)**: 15 items - MVP blockers affecting core functionality
  - **MUST COMPLETE FIRST**: Test Infrastructure Setup (3 weeks)
- **P1 (High)**: 27 items - Important improvements for user experience and stability  
- **P2 (Medium)**: 35 items - Quality improvements and technical debt

### Testing Requirements
- **Current State**: 44 existing test functions across 9 test files (as of December 2025)
- **Target State**: 70%+ code coverage minimum, 85%+ target
- **Approach**: Test-Driven Development (TDD) for all TODO implementations
- **Timeline Impact**: +3 weeks for test infrastructure, but prevents costly regressions

### Timeline Overview
The TODO items span multiple areas of the application:
- **LinkedIn API Integration** (scraping, posting, engagement)
- **User Interaction & Messaging** (DMs, notifications, recommendations)
- **Database & Configuration** (schema, settings, tokens)
- **Content Creation** (carousels, posts, AI generation)
- **Infrastructure** (AWS CDK, Selenium, scaling)
- **Code Quality** (documentation, cleanup, performance)

---

## Testing Strategy & Requirements

### Current Test Coverage Analysis

The LinkedIn Engagement Manager currently has **44 test functions** across multiple test files:

**Existing Test Files:**
- `tests/db_tests.py` - Database operations (post status, video URLs, content updates)
- `tests/ai_tests.py` - AI integration (image/video generation, prompts)
- `tests/date_tests.py` - Date/time handling and timezone conversions
- `tests/run_automation_test.py` - Automation workflows
- `tests/run_content_plan_test.py` - Content planning functionality
- `tests/simple_automation_tests.py` - Basic automation scenarios
- `tests/chrome_test.py` - Selenium/Chrome integration
- `tests/aws_test.py` - AWS infrastructure
- `tests/unit/test_cdk_stack.py` - AWS CDK stack validation

### Test Infrastructure Improvements (P0 - Must Complete Before New Development)

**Priority**: P0 - Critical Foundation  
**Estimated Effort**: 3 weeks  
**Business Impact**: Prevents regression and ensures code quality during TODO implementation

#### Required Test Improvements:

1. **Establish Test Running Infrastructure**
   - Set up CI/CD pipeline with automated test execution
   - Configure pytest with proper test discovery and markers
   - Add test coverage reporting (target: 70%+ coverage)
   - Document test running commands in README
   - Estimated: 3-5 days

2. **Create Comprehensive Unit Tests for Core Utilities**
   - `src/cqc_lem/utilities/db.py` - All database operations
   - `src/cqc_lem/utilities/linkedin/scrapper.py` - Profile scraping functions
   - `src/cqc_lem/utilities/linkedin/poster.py` - Post creation and publishing
   - `src/cqc_lem/utilities/carousel_creator.py` - Carousel generation
   - `src/cqc_lem/utilities/ai/ai_helper.py` - AI prompt generation and content creation
   - Estimated: 1-2 weeks

3. **Add Integration Tests for Critical Workflows**
   - End-to-end automation workflows (connection requests, engagement, DMs)
   - Content creation and scheduling pipeline
   - LinkedIn API interaction flows
   - Estimated: 1 week

4. **Implement Test Fixtures and Mocks**
   - Mock LinkedIn API responses to avoid rate limits
   - Database fixtures for consistent test data
   - AI service mocks for predictable testing
   - Selenium/WebDriver mocks for UI testing
   - Estimated: 3-5 days

5. **Add Regression Tests for Known Issues**
   - Test cases for each TODO item before implementation
   - Test cases to verify TODO fixes don't break existing functionality
   - Estimated: Ongoing per TODO

### Testing Requirements Per Issue Group

#### Group 1: LinkedIn API Error Handling & Core Features
**Test Requirements:**
- [ ] Unit tests for scrapper methods (including edge cases like empty prefix)
- [ ] Mock LinkedIn responses for reliable testing
- [ ] Integration tests for profile data collection
- [ ] Tests for error handling and retry logic
- [ ] Performance tests for batch scraping operations

#### Group 2: User Interaction & Notification System
**Test Requirements:**
- [ ] Unit tests for DM composition and personalization
- [ ] Integration tests for message sending workflows
- [ ] Tests for message history retrieval and duplicate detection
- [ ] Mock tests for various user profile types
- [ ] Tests for notification triggers and delivery

#### Group 3: Database Schema & Models
**Test Requirements:**
- [ ] Unit tests for all database operations (CRUD)
- [ ] Tests for token expiration filtering
- [ ] Tests for Enum handling across different contexts
- [ ] Tests for active user detection logic
- [ ] Database migration tests
- [ ] Tests for connection invite logging

#### Group 4: Configuration & Settings Management
**Test Requirements:**
- [ ] Unit tests for configuration loading and validation
- [ ] Tests for environment variable handling
- [ ] Tests for user-specific settings retrieval
- [ ] Tests for AWS SQS Celery backend configuration
- [ ] Tests for consent and compliance checks

#### Group 5: Content Processing & Validation
**Test Requirements:**
- [ ] Unit tests for each carousel type handler
- [ ] Tests for image grabber integration (Pexels/other services)
- [ ] Tests for content validation and sanitization
- [ ] Tests for post type limits and restrictions
- [ ] Tests for AI preference learning
- [ ] Tests for reaction type validation

#### Group 6: Infrastructure & AWS Configuration
**Test Requirements:**
- [ ] Unit tests for CDK stack definitions
- [ ] Tests for resource allocation (CPU, memory)
- [ ] Tests for auto-scaling policies
- [ ] Tests for service discovery (Cloud Map)
- [ ] Tests for EFS mount point configuration
- [ ] Integration tests for Selenium Grid setup
- [ ] Tests for production vs development configuration flags

#### Group 7: Testing & Code Quality
**Test Requirements:**
- [ ] Tests for performance metrics collection
- [ ] Tests for de-duplication logic
- [ ] Load tests for concurrent task execution

#### Group 8: Documentation & Enhancement
**Test Requirements:**
- [ ] Tests for textract alternatives
- [ ] UI component tests (Streamlit pages)
- [ ] Tests for pricing structure calculations

### Test Development Workflow

**CRITICAL RULE**: Before implementing any TODO item, the following test workflow MUST be followed:

1. **Pre-Implementation Phase**
   - Write failing tests that demonstrate the current broken/missing behavior
   - Document expected behavior in test assertions
   - Ensure tests are runnable (even if failing)

2. **Implementation Phase**
   - Implement the TODO fix/feature
   - Run tests frequently to verify progress
   - Refactor code while maintaining passing tests

3. **Validation Phase**
   - All new/modified tests must pass
   - No existing tests should break (regression check)
   - Code coverage should maintain or improve
   - Run full test suite before marking TODO as complete

4. **Documentation Phase**
   - Update test documentation
   - Add comments for complex test scenarios
   - Document any test limitations or known issues

### Test Commands

```bash
# Set up virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install test dependencies
poetry install --with dev

# Run all tests
poetry run pytest

# Run tests with coverage
poetry run pytest --cov=src/cqc_lem --cov-report=html --cov-report=term

# Run specific test file
poetry run pytest tests/db_tests.py

# Run tests matching pattern
poetry run pytest -k "test_scrapper"

# Run tests with verbose output
poetry run pytest -v

# Run tests and stop on first failure
poetry run pytest -x

# Run tests in parallel (faster)
poetry run pytest -n auto
```

### Success Metrics for Testing

- **Code Coverage**: Maintain minimum 70% coverage, target 85%+
- **Test Execution Time**: Full test suite < 5 minutes
- **Test Reliability**: Zero flaky tests, 100% deterministic results
- **Regression Prevention**: All TODO fixes must include tests that would catch regression
- **Documentation**: Every test file has clear docstrings explaining what is tested

---

## Priority-Based Issue Groups

### Group 1: LinkedIn API Error Handling & Core Features (P0 - Critical)

**Priority**: P0 - Critical MVP Blocker  
**Estimated Effort**: 2-3 weeks  
**Business Impact**: Core functionality failures block user engagement  

#### TODO Items:
1. **Fix scrapper method** (`src/cqc_lem/utilities/linkedin/scrapper.py:238`)
   - TODO: Fix this method
   - Impact: Broken scraping functionality affects data collection

2. **Handle empty prefix in scrapper** (`src/cqc_lem/utilities/linkedin/scrapper.py:344`)
   - TODO: Fix for when prefix is empty
   - Impact: Runtime errors when processing profiles

3. **Implement poster functionality** (`src/cqc_lem/utilities/linkedin/poster.py:164`)
   - TODO: Implement and test this
   - Impact: Core posting feature incomplete

4. **Get profile awards** (`src/cqc_lem/utilities/linkedin/scrapper.py:115`)
   - TODO: Get the awards
   - Impact: Incomplete profile data collection

5. **Get profile interests** (`src/cqc_lem/utilities/linkedin/scrapper.py:116`)
   - TODO: Get Interest (top voices, companies, groups, newsletters)
   - Impact: Missing engagement targeting data

---

### Group 2: User Interaction & Notification System (P0 - Critical)

**Priority**: P0 - Critical MVP Feature  
**Estimated Effort**: 3-4 weeks  
**Business Impact**: Core engagement and messaging features  

#### TODO Items:
1. **Implement DM sending after recommendations** (`src/cqc_lem/app/run_automation.py:748`)
   - TODO: After Receiving a Recommendation:
   - Impact: Missing automated thank-you messages

2. **Implement DM after interviews** (`src/cqc_lem/app/run_automation.py:750`)
   - TODO: After an Interview:
   - Impact: Missing follow-up automation

3. **Implement DM for collaborations** (`src/cqc_lem/app/run_automation.py:752`)
   - TODO: For a Successful Collaboration:
   - Impact: Missing relationship nurturing

4. **Enable send_private_dm task** (`src/cqc_lem/app/run_automation.py:757-759`)
   - TODO: Update profile_url, message, and enable send_private_dm.apply_async
   - Impact: DM functionality not operational

5. **Send value-based DMs** (`src/cqc_lem/app/run_automation.py:1074`)
   - TODO: Send DM - offer something of value
   - Impact: Missing engagement value proposition

6. **Generate value offerings** (`src/cqc_lem/app/run_automation.py:1075`)
   - TODO: Generate something of value to offer
   - Impact: No automated content for outreach

7. **Retrieve previous messages** (`src/cqc_lem/app/run_automation.py:1080`)
   - TODO: Review/Retrieve previous messages with user first
   - Impact: Risk of duplicate or irrelevant messages

8. **Check user profile for message focus** (`src/cqc_lem/app/run_automation.py:1083`)
   - TODO: Check user profile to find what type of focus the message should have
   - Impact: Generic, non-targeted messaging

9. **Get initial message from profile** (`src/cqc_lem/app/run_automation.py:1086`)
   - TODO: Get initial message from user profile
   - Impact: Missing personalized outreach

10. **Implement method (incomplete)** (`src/cqc_lem/app/run_automation.py:1146`)
    - TODO: Implement this method and
    - Impact: Incomplete functionality

---

### Group 3: Database Schema & Models (P1 - High)

**Priority**: P1 - High Impact  
**Estimated Effort**: 1-2 weeks  
**Business Impact**: Data integrity and query performance  

#### TODO Items:
1. **Add token expiration filter** (`src/cqc_lem/utilities/db.py:229`)
   - TODO: Add where clause to only return non-expired tokens
   - Impact: May return expired authentication tokens

2. **Fix Enum handling** (`src/cqc_lem/utilities/db.py:405`)
   - TODO: Why Enum doesn't work inside this function
   - Impact: Type safety issues, potential bugs

3. **Implement active user detection** (`src/cqc_lem/utilities/db.py:833`)
   - TODO: Update this when you have a way to see who is active (timestamp of login or paid)
   - Impact: Cannot target active users properly

4. **Add invite logging** (`src/cqc_lem/app/run_automation.py:1230`)
   - TODO: Add log entry for successful and failed invites to connect
   - Impact: No audit trail for connection invites

---

### Group 4: Configuration & Settings Management (P1 - High)

**Priority**: P1 - High Impact  
**Estimated Effort**: 2 weeks  
**Business Impact**: Operational efficiency and flexibility  

#### TODO Items:
1. **Make coordinates configurable** (`src/cqc_lem/utilities/selenium_util.py:100`)
   - TODO: Get this from function parameter
   - Impact: Hard-coded values reduce flexibility

2. **Get coordinates from database** (`src/cqc_lem/utilities/selenium_util.py:359`)
   - TODO: Get the coordinates from the user's entry in the database
   - Impact: Cannot customize per-user settings

3. **Verify AI helper path** (`src/cqc_lem/utilities/ai/ai_helper.py:1634`)
   - TODO: Verify this final path and move
   - Impact: Potential path configuration issues

4. **Update Celery backend for AWS SQS** (`src/cqc_lem/app/my_celery.py:36`)
   - TODO: What should this be for AWS SQS
   - Impact: Celery backend may not work in AWS

5. **Centralize namespace configuration** (`src/cqc_lem/aws/cdk/batch/celery_batch_worker_stack.py:146`)
   - TODO: Need this somewhere central
   - Impact: Hard-coded namespace values

6. **Uncomment production code** (`src/cqc_lem/aws/app.py:34`)
   - TODO: Uncomment below
   - Impact: Missing production configuration

7. **Implement My Account function** (`src/cqc_lem/streamlit/pages/1_My_Account.py:12`)
   - TODO: Implement this function
   - Impact: Account page non-functional

8. **Check LinkedIn data consent** (`src/cqc_lem/streamlit/pages/1_My_Account.py:53`)
   - TODO: Check DB for user and see if they need to re-consent to LinkedIn Data
   - Impact: GDPR/compliance risk

---

### Group 5: Content Processing & Validation (P1 - High)

**Priority**: P1 - High Impact  
**Estimated Effort**: 2-3 weeks  
**Business Impact**: Content quality and user experience  

#### TODO Items:
1. **Handle PersonalStoryCarousel** (`src/cqc_lem/utilities/carousel_creator.py:167`)
   - TODO: Handle PersonalStoryCarousel
   - Impact: Missing carousel type

2. **Handle IndustryInsightsCarousel** (`src/cqc_lem/utilities/carousel_creator.py:170`)
   - TODO: Handle IndustryInsightsCarousel
   - Impact: Missing carousel type

3. **Handle EventRecapCarousel** (`src/cqc_lem/utilities/carousel_creator.py:173`)
   - TODO: Handle EventRecapCarousel
   - Impact: Missing carousel type

4. **Handle TestimonialCarousel** (`src/cqc_lem/utilities/carousel_creator.py:176`)
   - TODO: Handle TestimonialCarousel
   - Impact: Missing carousel type

5. **Handle ProductDemoCarousel** (`src/cqc_lem/utilities/carousel_creator.py:208`)
   - TODO: Handle ProductDemoCarousel
   - Impact: Missing carousel type

6. **Implement two-column layout** (`src/cqc_lem/utilities/carousel_creator.py:254`)
   - TODO: Figure out if and how to implement this one
   - Impact: Limited carousel layout options

8. **Add image grabber integration** (`src/cqc_lem/utilities/carousel_creator.py:264,279`)
   - TODO: Update this with something from pexels or other image grabber function
   - Impact: Manual image selection required
   - Note: Two locations need same integration (lines 264 and 279)

9. **Implement two-column slide layout** (`src/cqc_lem/utilities/carousel_creator.py:319`)
   - TODO: Figure how to implement this one
   - Impact: Limited slide layout options

10. **Delete legacy content creation** (`src/cqc_lem/app/run_content_plan.py:153`)
    - TODO: Delete below | Call the helper function
    - Impact: Code cleanup needed

11. **Clarify post type limits** (`src/cqc_lem/app/run_content_plan.py:215`)
    - TODO: Should we limit this to specific post_types?
    - Impact: Unclear content strategy

12. **Validate reaction types** (`src/cqc_lem/app/run_automation.py:308`)
    - TODO: Not sure if these are universal for all post
    - Impact: May use incorrect reaction types

13. **Use AI for preferences** (`src/cqc_lem/app/run_automation.py:311`)
    - TODO: Use AI to get preferences
    - Impact: Manual preference management

---

### Group 6: Infrastructure & AWS Configuration (P2 - Medium)

**Priority**: P2 - Medium Impact  
**Estimated Effort**: 3-4 weeks  
**Business Impact**: Production readiness and scalability  

#### TODO Items:
1. **Refine Selenium Node CPU** (`src/cqc_lem/aws/app.py:72`)
   - TODO: Refine these Selenium Node CPU values
   - Impact: Over/under-provisioned resources

2. **Refine Selenium Node Memory** (`src/cqc_lem/aws/app.py:73`)
   - TODO: Refine these Selenium Node values
   - Impact: Over/under-provisioned resources

3. **Remove ECS-EFS duplicate code (Code Cleanup)** (8 locations)
   - Files: `api_stack.py:108,111`, `web_stack.py:106,109`, `celery_beat_stack.py:120,123`, `celery_flower_stack.py:111,114`, `celery_worker_stack.py:138,141`
   - TODO: Code cleanup - remove comments stating "already handled - remove"
   - Impact: Code duplication comments need cleanup (functionality already implemented)
   - Note: These are cleanup tasks for obsolete comments, not functional TODOs

4. **Optimize Celery Flower resources** (`src/cqc_lem/aws/cdk/ecs/fargate_service/celery_flower_stack.py:24-25`)
   - TODO: Find out why celery flower needs so much CPU/memory
   - Impact: Over-provisioned resources

5. **Set production flag** (`src/cqc_lem/aws/cdk/ecs/fargate_service/celery_worker_stack.py:79`)
   - TODO: Turn this to true in production
   - Impact: Development settings in production

6. **Set max celery workers** (`src/cqc_lem/aws/cdk/ecs/fargate_service/celery_worker_stack.py:149`)
   - TODO: Find a good number for max celery workers capacity
   - Impact: Suboptimal scaling

7. **Increase max instances** (`src/cqc_lem/aws/cdk/shared_stack_props.py:58`)
   - TODO: Increase this to 10 or more once resources sizes are set correctly
   - Impact: Limited scaling capability

8. **Use cloud map URL for Selenium** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:139`)
   - TODO: Should we use the cloud map namespace url for host?
   - Impact: Service discovery issues

9. **Review deregistration timing** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:165`)
   - TODO: Review this deregistration value
   - Impact: Premature container termination

10. **Remove external ALB listener** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:176`)
    - TODO: Remove below - ALB shouldn't listen externally
    - Impact: Security risk

11. **Review hub scaling policy** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:243`)
    - TODO: Use below if need separate scaling policy for hub
    - Impact: Suboptimal scaling

12. **Add video recording** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:312`)
    - TODO: For Video Recording - (Mount folder and upload to S3 Bucket???)
    - Impact: Missing debugging capability

13. **Verify Selenium entrypoint** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:317`)
    - TODO: Check what the entrypoint and command should be
    - Impact: Container startup issues

14. **Remove node ALB target group** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:342`)
    - TODO: Node shouldn't need a target group that is used by an ALB listener
    - Impact: Unnecessary resources

15. **Review node deregistration** (`src/cqc_lem/aws/cdk/batch/selenium_stack.py:361`)
    - TODO: Review this deregistration value
    - Impact: Premature container termination

16. **Adjust batch worker CPU** (`src/cqc_lem/aws/cdk/batch/celery_batch_worker_stack.py:50`)
    - TODO: Put back to 1 if service doesn't start
    - Impact: Configuration uncertainty

17. **Adjust batch worker memory** (`src/cqc_lem/aws/cdk/batch/celery_batch_worker_stack.py:52`)
    - TODO: Put back to 2048 if service doesn't start
    - Impact: Configuration uncertainty

18. **Set production flag (batch)** (`src/cqc_lem/aws/cdk/batch/celery_batch_worker_stack.py:79`)
    - TODO: Turn this to true in production
    - Impact: Development settings in production

19. **Verify EFS mount point** (`src/cqc_lem/aws/cdk/batch/celery_batch_worker_stack.py:83`)
    - TODO: Volume added but no mount point
    - Impact: Assets may not be accessible

20. **Add Selenium node mount points** (`src/cqc_lem/aws/cdk/efs/efs_stack.py:97`)
    - TODO: Add mount points to Selenium Nodes
    - Impact: Selenium cannot access shared storage

21. **Clarify Redis DB usage** (`src/cqc_lem/aws/cdk/main_stack.py:60`)
    - TODO: Should this redis db be hardcoded?
    - Impact: Configuration flexibility

---

### Group 7: Testing & Code Quality (P2 - Medium)

**Priority**: P2 - Medium Impact  
**Estimated Effort**: 1-2 weeks  
**Business Impact**: Code maintainability and debugging  

#### TODO Items:
1. **Add de-duplication logic** (`src/cqc_lem/app/run_automation.py:1420`)
   - TODO: If this is still running 4 times then add de-duplication logic
   - Impact: Duplicate task execution

---

### Group 8: Documentation & Enhancement (P2 - Medium)

**Priority**: P2 - Medium Impact  
**Estimated Effort**: 2-3 weeks  
**Business Impact**: User experience and feature completeness  

#### TODO Items:
1. **Find textract alternative** (`src/cqc_lem/streamlit/utils.py:353`)
   - TODO: Need to find alternative to textract
   - Impact: Dependency conflict blocks functionality

2. **Emulate commentify.co UI** (`src/cqc_lem/streamlit/Home.py:23`)
   - TODO: Emulate the main page from http://commentify.co - Develop a similar pricing structure
   - Impact: Missing professional landing page

---

## GitHub Issue Template

Use the following template when creating individual issues from these TODO items:

```markdown
## [Component] TODO Title

**Priority**: P0/P1/P2  
**Category**: [API/Database/Infrastructure/Content/UI/Testing]  
**Estimated Effort**: [hours/days/weeks]  
**Dependencies**: [List any related issues]

### Description
[Detailed description of the TODO item]

### Current Behavior
[What happens now]

### Expected Behavior
[What should happen]

### Location
- **File**: `path/to/file.py`
- **Line**: [line number]
- **Function/Method**: [if applicable]

### Acceptance Criteria
- [ ] [Specific testable outcome 1]
- [ ] [Specific testable outcome 2]
- [ ] [Specific testable outcome 3]

### Implementation Notes
[Any technical details, considerations, or approach suggestions]

### Testing Requirements
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] Manual testing completed

### Related TODOs
[List any related TODO items that should be addressed together]
```

---

## Copilot Automation Prompt

To enable Copilot to automatically process these TODO items, use the following prompt:

```
@copilot I need you to systematically address TODO items from the project timeline.

**CRITICAL**: Before implementing any TODO, you MUST first ensure proper test coverage exists.

Process:
1. Review the TODO_PROJECT_TIMELINE.md document
2. Review the "Testing Strategy & Requirements" section
3. For the next highest priority TODO:
   a. Check if adequate tests exist for the affected code
   b. If tests are missing, write them FIRST (they should fail)
   c. Implement the TODO fix/feature
   d. Verify all tests pass (new and existing)
   e. Check code coverage hasn't decreased
4. Create an implementation plan with:
   - Current test coverage assessment
   - Required new tests (written before implementation)
   - Clear acceptance criteria
   - Testing strategy
   - Risk assessment
5. Implement the changes following Test-Driven Development
6. Run relevant tests to validate
7. Run full test suite to prevent regression
8. Report progress with commit
9. Move to next TODO item

**Test-First Approach**: Always write failing tests before implementing fixes. This ensures:
- Clear specification of expected behavior
- Regression prevention
- Confidence in the fix

Please start with the Testing Infrastructure improvements, then proceed to Group 1 (LinkedIn API Error Handling & Core Features) and work through items in priority order.
```

---

## Implementation Roadmap

### Phase 0: Test Foundation (Weeks 1-3) **[MUST COMPLETE FIRST]**
- **Week 1**: Set up test infrastructure and CI/CD pipeline
- **Week 2**: Create comprehensive unit tests for core utilities
- **Week 3**: Add integration tests and test fixtures

**Deliverables:**
- ✅ Automated test execution in CI/CD
- ✅ 70%+ code coverage on core modules
- ✅ Documented test running procedures
- ✅ Mock infrastructure for LinkedIn API and external services

### Phase 1: Critical MVP (Weeks 4-9)
- **Weeks 4-6**: Group 1 - LinkedIn API Error Handling & Core Features
  - Write tests for each scrapper/poster method BEFORE fixing
  - Ensure all edge cases are covered (empty values, errors, etc.)
- **Weeks 7-9**: Group 2 - User Interaction & Notification System
  - Mock DM workflows for testing
  - Test message personalization and duplicate prevention

### Phase 2: Foundation & Stability (Weeks 10-14)
- **Weeks 10-11**: Group 3 - Database Schema & Models
  - Add database operation tests
  - Test migration and data integrity
- **Weeks 12-13**: Group 4 - Configuration & Settings Management
  - Test configuration loading and validation
  - Test environment-specific settings
- **Week 14**: Group 5 - Content Processing & Validation (Start)
  - Test carousel generation pipelines

### Phase 3: Content & Quality (Weeks 15-19)
- **Weeks 15-16**: Group 5 - Content Processing & Validation (Complete)
  - Test all carousel types
  - Test image grabber integration
- **Weeks 17-18**: Group 7 - Testing & Code Quality
  - Add performance and load tests
  - Implement de-duplication tests
- **Week 19**: Review and improve test coverage to 85%+

### Phase 4: Production & Infrastructure (Weeks 20-25)
- **Weeks 20-23**: Group 6 - Infrastructure & AWS Configuration
  - Test CDK stacks and resource allocation
  - Test auto-scaling and service discovery
- **Weeks 24-25**: Group 8 - Documentation & Enhancement
  - Test UI components
  - Final integration testing

---

## Updated Priority Distribution with Testing

### P0 (Critical) - Must Complete Before Other Work
1. **Test Infrastructure Setup** (Phase 0) - 3 weeks
2. **Group 1**: LinkedIn API Error Handling - 3 weeks (with tests)
3. **Group 2**: User Interaction & Notification System - 3 weeks (with tests)

### P1 (High) - Core Improvements
4. **Group 3**: Database Schema & Models - 2 weeks (with tests)
5. **Group 4**: Configuration & Settings Management - 2 weeks (with tests)
6. **Group 5**: Content Processing & Validation - 3 weeks (with tests)

### P2 (Medium) - Quality & Optimization
7. **Group 6**: Infrastructure & AWS Configuration - 4 weeks (with tests)
8. **Group 7**: Testing & Code Quality - 2 weeks
9. **Group 8**: Documentation & Enhancement - 2 weeks (with tests)

**Total Timeline**: 25 weeks with proper test coverage  
**Previous Timeline**: 18 weeks (excluding test infrastructure requirements)

---

## Success Metrics

### Test Quality (P0 - Critical)
- **Code Coverage**: Minimum 70% (baseline), Target 85%+ (goal)
- **Test Execution**: Full suite completes in < 5 minutes
- **Test Reliability**: 100% deterministic, zero flaky tests
- **Regression Prevention**: Every TODO fix includes test that prevents regression
- **Test Documentation**: All test files have clear docstrings
- **CI/CD Integration**: Automated test execution on every commit
- **Test-First Adoption**: 100% of TODO items implemented using TDD approach

### Code Quality
- All P0 TODO items resolved **with tests**
- 80%+ of P1 TODO items resolved **with tests**
- Test coverage maintained or improved (never decreased)
- No new TODO items introduced without corresponding issues **and tests**
- All existing tests continue to pass (zero regression)

### Business Impact
- Core LinkedIn engagement features operational **and tested**
- User messaging and DM automation functional **with integration tests**
- Production infrastructure stable and scalable **with validation tests**
- Improved user experience with complete carousel types **with unit tests**

### Technical Debt
- Reduced hard-coded configuration values
- Improved observability and logging **with performance tests**
- Cleaner, more maintainable codebase **validated by tests**
- Better documentation coverage
- Comprehensive test suite that enables confident refactoring

---

## Notes

- This document should be updated as TODO items are addressed
- New TODO items should be added to this timeline with proper prioritization
- Each completed TODO should reference the closing commit/PR
- Regular reviews (bi-weekly) should assess progress and adjust priorities

---

**Document Version**: 1.0  
**Last Updated**: 2025-12-28  
**Total TODO Items**: 77  
**Status**: Initial cataloging complete
