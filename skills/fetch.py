"""
Fetch skill — web/API data retrieval via domain-gated fetcher.

Commands:
  !fetch <url>                        — fetch a URL (domain-gated)
  !exchange <amount> <FROM> <TO>      — currency conversion
  !weather <city or lat,lon>          — current weather
"""

import json
import logging
from urllib.parse import quote

from skills.base import Skill
from tools.web_fetch import fetch, get_domain_policy

logger = logging.getLogger(__name__)


class FetchSkill(Skill):
    name = "fetch"
    description = "Fetch data from the web: URLs, currency exchange, weather."
    triggers = ["fetch", "exchange", "weather"]
    enabled = True

    async def execute(self, args: str, update, context) -> str:
        parts = args.strip().split(None, 1)
        if not parts:
            parts = [""]

        # Trigger logic is bit fuzzy here because we have multiple triggers mapping to one class.
        # We need to know WHICH trigger was used.
        # But the base execute doesn't receive the trigger.
        # Workaround: infer from message or args?
        # Or look at context if router passes it.
        # In my router.py, I am just calling skill.execute(args, ...)
        
        # We will try to guess based on the args or if the first word is a subcommand.
        
        # Note: The original generic FetchSkill seemed to rely on `context.current_trigger` or simply parsing.
        # I'll rely on parsing the message content if possible, but `args` here is usually "rest of message".
        
        # START HACK:
        # If the router stripped the command, `args` is the payload.
        # If I typed "!weather London", args="London".
        # If I typed "!fetch http...", args="http...".
        # If I typed "!exchange 100...", args="100...".
        
        # Use simple heuristics:
        
        arg_str = args.strip()
        
        # Valid URL -> fetch
        if (arg_str.startswith("http://") or arg_str.startswith("https://") or "www." in arg_str) and " " not in arg_str:
            return await self._fetch_url(arg_str, context)
            
        # 3 parts starting with number -> exchange
        # "100 USD EUR"
        ex_parts = arg_str.split()
        if len(ex_parts) == 3 and ex_parts[0].replace('.', '').isdigit():
             return await self._exchange(arg_str, context)
             
        # "exchange ..." -> exchange
        if arg_str.lower().startswith("exchange "):
            return await self._exchange(arg_str[9:], context)

        # "weather ..." -> weather
        if arg_str.lower().startswith("weather "):
            return await self._weather(arg_str[8:])
            
        # Default fallback: if it looks like a city, do weather? No, risky.
        # If we can't determine, return help.
        
        # However, checking the original code, it used `trigger = getattr(context, "current_trigger", None)`.
        # I should probably ensure my router sets that if I want this to work robustly.
        # For now, I'll check `context`.
        
        trigger = getattr(context, "current_trigger", None)
        if trigger == "weather":
            return await self._weather(arg_str)
        if trigger == "exchange":
            return await self._exchange(arg_str, context)
        if trigger == "fetch":
             return await self._fetch_url(arg_str, context)

        return self._help()

    async def _exchange(self, args: str, context) -> str:
        parts = args.split()
        if len(parts) < 3:
            return "Usage: `!exchange <amount> <FROM> <TO>`\nExample: `!exchange 100 USD JPY`"

        try:
            amount = float(parts[0].replace(",", ""))
            from_cur = parts[1].upper()
            to_cur = parts[2].upper()
        except ValueError:
            return "❌ Invalid amount. Usage: `!exchange 100 USD JPY`"

        CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "USDT", "USDC", "LTC", "LINK"}
        is_crypto = from_cur in CRYPTO_TICKERS or to_cur in CRYPTO_TICKERS

        if is_crypto:
            url = f"https://api.coinbase.com/v2/prices/{from_cur}-{to_cur}/spot"
            source = "Coinbase"
        else:
            url = f"https://api.frankfurter.app/latest?from={from_cur}&to={to_cur}&amount={amount}"
            source = "Frankfurter / ECB"

        policy, domain = get_domain_policy(url)
        if policy == "blocked":
            return f"❌ Fetch blocked for domain: `{domain}`"
        if policy == "confirm":
            context.set_pending_command(f"!fetch {url}")
            return (
                f"🌐 Fetch request: `{url}`\n"
                f"Domain `{domain}` requires confirmation.\n\n"
                "Reply `yes` to fetch or `no` to cancel."
            )

        try:
            raw = await fetch(url)
            data = json.loads(raw)
        except json.JSONDecodeError:
             return "❌ Exchange API returned invalid data. Check your tickers for typos."
        except Exception as e:
            return f"❌ Exchange API error (Check for typos): {e}"

        if is_crypto:
            if "errors" in data or "error" in data:
                return f"❌ Coinbase API Error: {url}"
            if "data" not in data or "amount" not in data["data"]:
                return f"❌ Invalid currency pair or typo: {from_cur}-{to_cur}"
            rate = float(data["data"]["amount"])
            total = rate * amount
            return (
                "💱 *Currency Exchange (Crypto)*\n"
                f"  {amount:,.4f} {from_cur} = *{total:,.2f} {to_cur}*\n"
                f"  Rate: 1 {from_cur} = {rate:,.2f} {to_cur}\n"
                f"  Source: {source}"
            )
        else:
            if "message" in data:
                return f"❌ API Error: {data['message']} (Are these valid fiat tickers?)"
            if "rates" not in data:
                return f"❌ Unexpected response or typo in tickers: {from_cur}, {to_cur}"
            rate = data["rates"].get(to_cur)
            if rate is None:
                return f"❌ Currency not found: {to_cur}"

            return (
                "💱 *Currency Exchange*\n"
                f"  {amount:,.2f} {from_cur} = *{rate:,.4f} {to_cur}*\n"
                f"  Rate: 1 {from_cur} = {rate / amount:,.6f} {to_cur}\n"
                f"  Source: {source}"
            )

    async def _weather(self, city: str) -> str:
        if not city:
            return "Usage: `!weather <city>`\nExample: `!weather Memphis TN`"

        city_encoded = quote(city)
        url = f"https://wttr.in/{city_encoded}?format=j1"
        policy, domain = get_domain_policy(url)
        if policy == "blocked":
            return f"❌ Fetch blocked: `{domain}`"

        try:
            raw = await fetch(url)
            if not raw or not raw.strip():
                return "❌ Weather API returned an empty response. It may be down."
            data = json.loads(raw)
        except json.JSONDecodeError:
            if "Unknown location" in raw:
                return f"❌ Location not found: `{city}`"
            snippet = raw[:100].replace('\n', ' ')
            return f"❌ Weather API returned invalid format. Is the API down?\nResponse: `{snippet}`"
        except Exception as e:
            return f"❌ Weather API fetch error: {e}"

        try:
            current = data["current_condition"][0]
            temp_f = current.get("temp_F", "?")
            temp_c = current.get("temp_C", "?")
            feels_f = current.get("FeelsLikeF", "?")
            humidity = current.get("humidity", "?")
            desc = current.get("weatherDesc", [{}])[0].get("value", "?")
            wind_mph = current.get("windspeedMiles", "?")
            wind_dir = current.get("winddir16Point", "?")
        except (KeyError, IndexError) as e:
            return f"❌ Weather data format missing expected fields: {e}\nSnippet: {raw[:150]}"

        return (
            f"🌤 *Weather: {city}*\n"
            f"  {desc}\n"
            f"  🌡 {temp_f}°F / {temp_c}°C (feels like {feels_f}°F)\n"
            f"  💧 Humidity: {humidity}%\n"
            f"  💨 Wind: {wind_mph} mph {wind_dir}\n"
            "  Source: wttr.in"
        )

    async def _fetch_url(self, url: str, context) -> str:
        policy, domain = get_domain_policy(url)

        if policy == "blocked":
            return (
                f"⛔ Fetch blocked for `{domain}`.\n"
                "Internal addresses and metadata endpoints are not allowed."
            )

        if policy == "confirm":
            context.set_pending_command(f"!fetch {url}")
            return (
                f"🌐 Fetch request: `{url}`\n"
                f"Domain `{domain}` requires confirmation.\n\n"
                "Reply `yes` to fetch or `no` to cancel."
            )

        try:
            raw = await fetch(url)
        except Exception as e:
            return f"❌ Fetch failed: {e}"

        try:
            data = json.loads(raw)
            formatted = json.dumps(data, indent=2)
            if len(formatted) > 3000:
                formatted = formatted[:3000] + "\n...[truncated]"
            return f"```json\n{formatted}\n```"
        except json.JSONDecodeError:
            if len(raw) > 2000:
                raw = raw[:2000] + "\n...[truncated]"
            return f"```\n{raw}\n```"

    def _help(self) -> str:
        return (
            "🌐 *Fetch Commands*\n\n"
            "`!fetch <url>` — Fetch a URL (domain-gated)\n"
            "`!exchange <amount> <FROM> <TO>` — Currency conversion\n"
            "   Example: `!exchange 100 USD JPY`\n"
            "`!weather <city>` — Current weather\n"
            "   Example: `!weather Memphis TN`\n\n"
            "Unknown domains will ask for confirmation before fetching."
        )
