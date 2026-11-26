# 15. Provider abstraction и budgeting

`providers/base.py` определяет `LLMProvider`, `ProviderResponse`,
`ProviderCallIdentity`, `ProviderCapabilities`. `ProviderRegistry` выбирает provider заранее;
identity фиксируется до вызова. `TokenUsage` хранит токены и идентичность вызова;
`UsageService` пишет usage с provider/model/role/request/attempt, не по текущему registry.

Текущий budget gate — консервативные лимиты и usage accounting. Полная reservation-система
(`reserve → dispatch → reconcile → release`) не реализована; не заявляйте атомарный hard cap.

Вопрос: почему при timeout нельзя записывать 0 токенов?
