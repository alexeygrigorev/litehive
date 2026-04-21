# Claude Code Usage Tracking Implementation Specification

## Investigation Methodology
This analysis was conducted by examining the deobfuscated Claude Code source at https://github.com/yasasbanukaofficial/claude-code, specifically investigating the files required by the acceptance criteria:

1. **src/commands/usage/index.ts** - Command definition (delegates to usage.js)
2. **src/commands/usage/usage.tsx** - Component wrapper (delegates to Settings component)  
3. **src/commands/cost.ts** - Cost command interface (subscription status checks)
4. **src/cost-tracker.ts** - Core cost tracking implementation
5. **src/constants/oauth.ts** - OAuth configuration and endpoints
6. **src/constants/apiLimits.ts** - Media/document size constraints
7. **src/services/claudeAiLimits.ts** - Rate limit logic and thresholds
8. **src/services/api/usage.ts** - Primary usage API implementation
9. **src/services/api/ultrareviewQuota.ts** - Specialized quota endpoint

## Summary
**Verified from source**: Claude Code uses a hybrid approach for quota tracking: a primary OAuth-authenticated API endpoint for real-time usage data, supplemented by comprehensive local cost tracking for offline calculations and session persistence.

## Primary API Endpoint - Verified Implementation

### Main Usage/Quota API
**Source**: `src/services/api/usage.ts`
- **Endpoint**: `{BASE_API_URL}/api/oauth/usage`
- **Method**: GET
- **Authentication**: OAuth 2.0 Bearer token
- **Timeout**: 5 seconds
- **Validation**: Checks subscriber status and profile scope before request

### Base API URLs by Environment  
**Source**: `src/constants/oauth.ts`
- **Production**: `https://api.anthropic.com`
- **Staging**: `https://api-staging.anthropic.com` 
- **FedStart**: `https://claude.fedstart.com` (via CLAUDE_CODE_CUSTOM_OAUTH_URL)

### Authentication Method
**Source**: `src/constants/oauth.ts`
- Uses OAuth 2.0 tokens obtained via authorization code flow
- Token validation: checks expiration with buffer before requests via `isOAuthTokenExpired()`
- Headers: Content-Type: application/json + User-Agent + OAuth auth headers from `getAuthHeaders()`
- Client ID (Production): `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- Client ID (Staging): `22422756-60c9-4084-8eb7-27705fd5cf9a`

## Response Structure

### Utilization Object
```typescript
interface Utilization {
  // Rate limits with utilization percentage and reset timestamps
  five_hour: RateLimit;              // Current session limits
  seven_day: RateLimit;              // Weekly usage (all models)
  seven_day_oauth_apps: RateLimit;   // OAuth app specific
  seven_day_opus: RateLimit;         // Opus model weekly limit
  seven_day_sonnet: RateLimit;       // Sonnet model weekly limit
  
  // Extra usage (overage) tracking
  extra_usage: {
    enabled: boolean;
    monthly_limit: number;
    used_credits: number;
    utilization: number; // percentage
  }
}

interface RateLimit {
  utilization: number;    // 0-100 percentage
  resets_at: timestamp;   // when limit resets
}
```

## Rate Limit Types and Detection - Verified Implementation

### Five Rate Limit Categories
**Source**: `src/services/claudeAiLimits.ts`
1. **five_hour**: "session limit" - Short-term usage constraints
2. **seven_day**: "weekly limit" - Standard weekly usage windows  
3. **seven_day_opus**: "Opus limit" - Opus model-specific weekly limits
4. **seven_day_sonnet**: "Sonnet limit" - Sonnet model-specific weekly limits
5. **overage**: "extra usage limit" - Extra usage tier for paid plans

### Quota Status States
**Source**: `src/services/claudeAiLimits.ts`
- `allowed`: Standard usage permitted
- `allowed_warning`: Threshold approaching (early warning)
- `rejected`: Limit exceeded

### Early Warning System - Verified Thresholds
**Source**: `src/services/claudeAiLimits.ts`
- **Header-based detection**: Server sends `anthropic-ratelimit-unified-surpassed-threshold` headers (preferred method)
- **Fallback calculation**: Client computes time-relative warnings with specific thresholds:
  - **Five-Hour Window**: 90% utilization warning at 72% time elapsed
  - **Seven-Day Window**: 
    - 75% utilization warning at 60% time elapsed
    - 50% utilization warning at 35% time elapsed  
    - 25% utilization warning at 15% time elapsed
- **Logic**: Warns users consuming quota "faster than the time window allows"

## Secondary Endpoints - Verified Implementation

### Ultrareview Quota (Specialized Feature)
**Source**: `src/services/api/ultrareviewQuota.ts`
- **Endpoint**: `{BASE_API_URL}/v1/ultrareview/quota`
- **Method**: GET
- **Authentication**: OAuth access token via `getOAuthHeaders()` + `x-organization-uuid` header
- **Timeout**: 5 seconds
- **Fallback**: Returns null when not subscriber or endpoint errors
- **Response**:
  ```typescript
  interface UltrareviewQuotaResponse {
    reviews_used: number;
    reviews_limit: number;
    reviews_remaining: number;
    is_overage: boolean;
  }
  ```

## Local Tracking Implementation - Verified Implementation

### Cost Tracking Functions
**Source**: `src/cost-tracker.ts`
- `addToTotalSessionCost()`: Accumulates costs and usage metrics
- `addToTotalModelUsage()`: Updates token counts and consolidates by model "short name"
- `formatTotalCost()`: Formats currency with dynamic precision (referenced in cost.ts)
- `getModelUsage()`: Retrieves aggregated usage statistics by model
- `calculateUSDCost()`: Computes costs by model and token usage

### Tracked Metrics - Verified Token Types
**Source**: `src/cost-tracker.ts`
- **Token Types**: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- **Web Search**: `server_tool_use.web_search_requests`
- **Timing**: API duration vs. tool duration
- **Code Changes**: Lines added/removed
- **Cost**: Total USD amount
- **Analytics**: Telemetry events with `cost_usd_micros` for detailed usage analysis

### Session Persistence - Verified Storage
**Source**: `src/cost-tracker.ts`
- **Storage Location**: Project configuration file
- **Stored Fields**: `lastCost`, `lastSessionId`, `lastModelUsage`, `lastAPIDuration`, `lastToolDuration`, token totals
- **Restoration Logic**: `restoreCostStateForSession()` only restores when session IDs match to prevent cross-session data corruption

## Quota Checking Strategy

### Proactive Detection Implementation
1. **Real-time API checks**: Call `/api/oauth/usage` endpoint before expensive operations
2. **Test query method**: Use `makeTestQuery()` with small, fast model for quota verification
3. **Header monitoring**: Parse response headers for threshold breach notifications
4. **Local calculation backup**: Track usage locally when API unavailable
5. **Early warning triggers**: Implement percentage-based warnings before hard limits

### Error Handling
- Token expiration: Auto-refresh using refresh token
- API unavailable: Fall back to local tracking estimates
- Network timeout: 5-second timeout with graceful degradation
- Invalid response: Return null rather than throwing exceptions

## Follow-up Implementation Task - Exact Specification

Based on the verified source code analysis, here is the concrete implementation specification for proactive Claude Code quota detection:

### Primary Implementation: OAuth Usage API Integration

**Required API Integration**:
- **Endpoint**: `{BASE_API_URL}/api/oauth/usage` (verified in `src/services/api/usage.ts`)
- **Authentication**: OAuth 2.0 Bearer token with subscription validation
- **Response Fields**: `five_hour`, `seven_day`, `seven_day_oauth_apps`, `seven_day_opus`, `seven_day_sonnet`, `extra_usage`
- **Fallback**: Return null on error, graceful degradation

### Implementation Steps

**Step 1: Replicate Claude Code's Usage API Pattern**
```typescript
// Based on src/services/api/usage.ts
async function fetchQuotaStatus(): Promise<Utilization | null> {
  // Validate subscriber status and profile scope
  // Check OAuth token expiration with buffer
  // Call {BASE_API_URL}/api/oauth/usage with 5s timeout
  // Parse response for rate limit utilization percentages
}
```

**Step 2: Implement Verified Warning Logic** 
```typescript
// Based on src/services/claudeAiLimits.ts early warning thresholds
function calculateQuotaWarning(utilization: number, timeElapsed: number, limitType: string): QuotaStatus {
  // Five-hour: 90% utilization warning at 72% time elapsed
  // Seven-day: 75%/60%, 50%/35%, 25%/15% utilization/time thresholds
  // Return 'allowed', 'allowed_warning', or 'rejected'
}
```

**Step 3: OAuth Configuration**
- **Client ID (Prod)**: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- **Base URL**: `https://api.anthropic.com` (production)
- **Headers**: Content-Type: application/json + User-Agent + OAuth auth headers

### Alternative Implementation: Local Cost Tracking

**When API Unavailable**: Integrate with cost-tracker.ts pattern for local estimation
- **Token Tracking**: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
- **Session Persistence**: Store in project configuration file
- **Cost Calculation**: Use `calculateUSDCost()` for budget-based warnings

## Subscription Tier Limits - Source Investigation Results

### Findings from Deobfuscated Source Analysis
**Investigation Sources**:
- `src/constants/apiLimits.ts`: Contains only media/document size constraints, explicitly notes "Future: See issue #13240 for dynamic limits fetching from server" 
- `src/services/claudeAiLimits.ts`: Contains relative thresholds and warning triggers but no absolute numerical limits
- `src/services/api/usage.ts`: Implements API calls but server provides the actual limit values

### Key Discovery
**Exact numerical subscription tier limits are NOT hardcoded in the client source code.** The implementation uses a server-driven approach where:
1. Client calls `/api/oauth/usage` endpoint 
2. Server returns current utilization percentages and reset timestamps
3. Client calculates warnings based on percentage thresholds, not absolute limits

### Observable Tier Differentiation
**Source**: `src/services/claudeAiLimits.ts`
- **Overage Access**: 12 documented reasons for overage restrictions including "overage_not_provisioned", organizational limits, insufficient credits, seat tier restrictions
- **Rate Limit Types**: Different subscription tiers have access to different combinations of the five rate limit types
- **Model-Specific Limits**: Higher tiers get separate `seven_day_opus` and `seven_day_sonnet` quotas

### Implementation Recommendation
To obtain exact tier limits, the implementation would need to:
1. Call the `/api/oauth/usage` endpoint for authenticated users
2. Parse the returned rate limit objects to extract current limits
3. Map subscription status (from `isClaudeAISubscriber()`) to understand tier capabilities

## Security Considerations
- OAuth tokens stored securely in system credential store
- 5-minute token expiration buffer prevents auth failures
- No quota data cached beyond necessary for UX
- Client-side validation only for UX; server enforces actual limits

## Testing Strategy
- Mock quota responses for different limit states
- Test token refresh flows
- Verify fallback behavior when API unavailable
- Load test quota checking performance impact