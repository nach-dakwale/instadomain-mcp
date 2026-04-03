# InstaDomain

[![instadomain-mcp MCP server](https://glama.ai/mcp/servers/nach-dakwale/instadomain-mcp/badges/score.svg)](https://glama.ai/mcp/servers/nach-dakwale/instadomain-mcp)

**Domain registration for AI agents.** Check availability, buy domains, and configure DNS without leaving your terminal.

InstaDomain is an MCP server that lets AI coding assistants (Claude Code, Cursor, Windsurf) register domains on your behalf. Pay with Stripe or x402 (USDC on Base). Domains are registered in your name with Cloudflare DNS auto-configured.

## Why?

You're building a project in Claude Code. You need a domain. Today you alt-tab to Namecheap, search, add to cart, fill out forms, configure DNS. That's 10 minutes of context-switching.

With InstaDomain, your AI agent checks availability, shows you the price, and buys it in one conversation. DNS is ready before you finish your next prompt.

## Features

- **Check domains** - single domain with pricing, bulk check up to 50, or AI-generated suggestions
- **Buy domains** - Stripe checkout (card) or x402 (USDC on Base, no signup required)
- **Automatic DNS** - Cloudflare zone created with scoped API token returned to you
- **Your domain, your name** - registered as your property, not ours
- **Transfer out anytime** - EPP auth codes and domain unlock built in
- **Auto-renew** - enabled by default, renew via Stripe
- **WHOIS privacy** - enabled by default

## MCP Tools

| Tool | What it does |
|------|-------------|
| `check_domain` | Check availability + price for a single domain |
| `check_domains_bulk` | Check up to 50 domains at once |
| `suggest_domains` | Generate and check domain name ideas for a keyword |
| `buy_domain` | Purchase via Stripe checkout |
| `buy_domain_crypto` | Purchase via x402 USDC payment |
| `get_domain_status` | Poll order status until complete |
| `get_transfer_code` | Get EPP auth code to transfer your domain away |
| `unlock_domain` | Remove registrar lock for transfers |
| `renew_domain` | Renew via Stripe |

## Quick Start

### Smithery (recommended)

```bash
npx -y @smithery/cli@latest install @nach-dakwale/instadomain --client claude
```

### Claude Code

Add to your MCP config:

```json
{
  "mcpServers": {
    "instadomain": {
      "url": "https://instadomain.fly.dev/mcp/"
    }
  }
}
```

### Cursor / Windsurf

Use the Streamable HTTP endpoint:

```
https://instadomain.fly.dev/mcp/
```

## Example

```
You: Find me a domain for a coffee subscription service

Claude: Let me check some options...
        coffeecrate.com - $18.12 (available)
        brewbox.io - $52.49 (available)
        dailygrind.co - $35.99 (available)

You: Buy coffeecrate.com

Claude: I'll need your registration details...
        [collects name, email, address]

        Stripe checkout created. Complete payment here:
        https://checkout.stripe.com/...

        Domain registered! DNS zone created on Cloudflare.
        Your scoped API token: cf_...
```

## Pricing

Wholesale cost + small flat markup per TLD. Examples:

| TLD | Price |
|-----|-------|
| .com | ~$18 |
| .io | ~$52 |
| .dev | ~$21 |
| .co | ~$36 |
| .ai | ~$99 |

Prices include 1-year registration and WHOIS privacy.

## x402 Crypto Payments

InstaDomain accepts x402 payments in USDC on Base. No signup, no API keys. Your AI agent pays directly from a wallet.

This enables fully autonomous agents to register domains without human payment intervention.

## REST API

InstaDomain also exposes a REST API for non-MCP integrations:

- `GET /check/{domain}` - check availability + price
- `POST /check` - bulk check (up to 50 domains)
- `GET /suggest?keyword=coffee` - AI-generated domain suggestions
- `POST /buy` - initiate Stripe purchase
- `GET /status/{order_id}` - check order status
- `POST /renew/{order_id}` - renew a domain

Full API at `https://instadomain.fly.dev/docs`

## Legal

Domains are registered through OpenSRS (Tucows). You are the legal registrant. InstaDomain is listed as admin/tech contact for service management only. Full ICANN rights apply, including the right to transfer your domain at any time.

- [Terms of Service](https://instadomain.fly.dev/terms)
- [Privacy Policy](https://instadomain.fly.dev/privacy)

## Links

- **Live server:** https://instadomain.fly.dev
- **Smithery:** https://smithery.ai/servers/nach-dakwale/instadomain
- **MCP endpoint:** https://instadomain.fly.dev/mcp/

## License

MIT
