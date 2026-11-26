# Provider lifecycle

1. Registry resolves canonical role to provider before a call.
2. `ProviderCallIdentity` is fixed and carried on `ProviderResponse`.
3. Adapter maps provider errors to normalized classes and Retry-After.
4. Quota state is provider-scoped and persisted in `provider_states`.
5. `models.list` health does not prove generation; quota remains until a safe recovery probe.
6. No automatic paid-provider fallback.
