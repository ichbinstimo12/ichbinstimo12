import asyncio
import os
from dotenv import load_dotenv
from src.firm.broker.client import CapitalComClient
from src.firm.research.feasibility import FeasibilityAnalyzer

async def main():
    load_dotenv()

    # Use .env variables
    api_key = os.getenv("CAPITAL_API_KEY")
    identifier = os.getenv("CAPITAL_IDENTIFIER")
    password = os.getenv("CAPITAL_PASSWORD")

    if not all([api_key, identifier, password]):
        print("Error: Missing API keys in .env")
        return

    client = CapitalComClient(api_key, identifier, password)

    print("Authenticating with Capital.com...")
    if await client.authenticate():
        # Use the balance from the account for accurate analysis
        account_info = await client.get_account_details()
        # Extract balance safely (depends on actual API response structure)
        balance = account_info.get("balance", 200.0)
        print(f"Account Balance detected: {balance} CHF")

        analyzer = FeasibilityAnalyzer(client, account_balance=balance)
        profiles = await analyzer.run_full_analysis()
        analyzer.print_report(profiles)

        await client.close()
    else:
        print("Authentication failed.")

if __name__ == "__main__":
    asyncio.run(main())
