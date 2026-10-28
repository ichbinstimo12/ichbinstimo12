import asyncio
import os
from dotenv import load_dotenv
from src.firm.broker.mock_client import MockBrokerClient
from src.firm.research.feasibility import FeasibilityAnalyzer

async def main():
    load_dotenv()

    api_key = os.getenv("CAPITAL_API_KEY", "mock_key")
    identifier = os.getenv("CAPITAL_IDENTIFIER", "mock_id")
    password = os.getenv("CAPITAL_PASSWORD", "mock_pass")

    print("--- RUNNING MOCK FEASIBILITY ANALYSIS ---")
    client = MockBrokerClient(api_key, identifier, password)

    if await client.authenticate():
        account_info = await client.get_account_details()
        balance = account_info.get("balance", 200.0)
        print(f"Mock Account Balance: {balance} CHF")

        analyzer = FeasibilityAnalyzer(client, account_balance=balance)
        profiles = await analyzer.run_full_analysis()
        analyzer.print_report(profiles)

        await client.close()
    else:
        print("Mock Authentication failed.")

if __name__ == "__main__":
    asyncio.run(main())
