import sys
import asyncio
import json
import csv
from tenacity import retry, wait_exponential, stop_after_attempt
from better_proxy import Proxy
import aiohttp
from tabulate import tabulate
from fake_useragent import UserAgent

from data.config import THREADS
from utils import logger

output_lock = asyncio.Lock()


class ConsoleTableFormatter:
    def __init__(self):
        self.headers = ["#", "Wallet Address", "Total Tokens", "Status"]
        self.results = []
        self.table_top_printed = False
        self.column_widths = [3, 18, 12, 8]

    async def add_result(self, index, wallet, tokens, status):
        self.results.append([index, wallet, tokens, status])
        await self.print_table_row()

    async def print_table_row(self):
        async with output_lock:
            if not self.table_top_printed:
                print(self.format_row(self.headers, is_header=True))
                self.table_top_printed = True

            new_row = self.results[-1]
            print(self.format_row(new_row))

            sys.stdout.flush()

    def format_row(self, row, is_header=False):
        formatted_row = []
        for i, (item, width) in enumerate(zip(row, self.column_widths)):
            if i == 0 or i == 2:
                formatted_item = str(item).rjust(width)
            else:
                formatted_item = str(item).ljust(width)
            formatted_row.append(formatted_item)

        if is_header:
            return f"| {' | '.join(formatted_row)} |"
        else:
            return f"| {' | '.join(formatted_row)} |"


class AirdropAllocator:
    def __init__(self, wallet_address: str, proxy: str = None, index: int = 0):
        self.wallet_address = wallet_address
        self.masked_wallet = f"{self.wallet_address[:6]}...{self.wallet_address[-6:]}"
        self.proxy = proxy and Proxy.from_str(proxy).as_url
        self.index = index
        self.base_url = 'https://api.getgrass.io/zvTlZ8PRouKKGTGNzg4k'
        self.headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US,en;q=0.9',
            'origin': 'https://www.grassfoundation.io',
            'priority': 'u=1, i',
            'referer': 'https://www.grassfoundation.io/',
            'sec-ch-ua': '"Not)A;Brand";v="99", "Google Chrome";v="127", "Chromium";v="127"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'cross-site',
            'user-agent': UserAgent().random,
        }

        self.results_table = []
        self.table_formatter = table_formatter

    @retry(wait=wait_exponential(min=1, max=2), stop=stop_after_attempt(15))  # Увеличено количество попыток до 15
    async def fetch_airdrop_allocation(self):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            async with session.get(f'{self.base_url}?input=%7B%22walletAddress%22:%22{self.wallet_address}%22%7D',
                                   headers=self.headers, proxy=self.proxy, verify_ssl=False) as response:

                data = await response.json()
                assert data.get('result')

                return data

    def calculate_totals(self, data):
        points = data.get('result', {}).get('data', {})
        total = sum(points.values())
        points['all'] = total
        return points

    def beautify_and_log(self, data, log_filename='airdrop_log.json'):
        with open(log_filename, 'a') as log_file:
            json.dump(data, log_file, indent=4)
            log_file.write('\n')

    def save_to_csv(self, data, filename='airdrop_allocation.csv'):
        result_data = data.get('result', {}).get('data', {})
        if not result_data:
            return

        # Чтение существующего CSV файла и получение уже записанных адресов кошельков
        existing_wallets = set()
        try:
            with open(filename, 'r') as file:
                reader = csv.reader(file)
                next(reader)  # Пропустить заголовок
                for row in reader:
                    existing_wallets.add(row[0])
        except FileNotFoundError:
            pass

        # Если кошелек уже есть в CSV, пропускаем его
        if self.wallet_address in existing_wallets:
            return

        # Дописываем данные в CSV файл
        with open(filename, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([self.wallet_address, result_data.get('all', 0)])

    async def format_console_output(self, data, status):
        global all_tokens

        total_tokens = data.get('all', 0)
        all_tokens += total_tokens

        if self.table_formatter:
            await self.table_formatter.add_result(self.index, self.masked_wallet, total_tokens, status)
        else:
            print(f"{self.index} | {self.masked_wallet} | {total_tokens} | {status}")

    async def process_allocation(self, log_filename='airdrop_log.json'):
        async with semaphore:
            try:
                data = await self.fetch_airdrop_allocation()
                totals = self.calculate_totals(data)

                if data.get('result', {}).get("data") is None:
                    await self.format_console_output({}, "Error")
                    return

                if any('_sybil' in key for key in totals):
                    status = "Sybil"

                    with open("logs/sybils.txt", "a") as f:
                        f.write(f"{self.wallet_address}\n")
                else:
                    status = "Eligible"
                    with open("logs/eligible.txt", "a") as f:
                        f.write(f"{self.wallet_address}\n")

                await self.format_console_output(totals, status)
                self.beautify_and_log(data, log_filename)
                self.save_to_csv(data)
            except Exception as e:
                await self.format_console_output({}, "Error")


async def read_file_lines(file_path):
    with open(file_path, 'r') as file:
        return [line.strip() for line in file if line.strip()]


async def print_table_headers():
    headers = ["#", "Wallet Address", "Total Tokens", "Status"]
    async with output_lock:
        print(tabulate([], headers=headers, tablefmt="grid"))


async def main():
    path = "data"
    wallet_addresses = await read_file_lines(f'{path}/wallets.txt')

    if not wallet_addresses:
        logger.info("No wallet addresses found!")
        return

    proxies = await read_file_lines(f'{path}/proxies.txt')

    tasks = []

    print("+-----+------------------+----------------+----------+")

    for i, wallet in enumerate(wallet_addresses):
        proxy = proxies[i % len(proxies)] if proxies else None
        allocator = AirdropAllocator(wallet_address=wallet, proxy=proxy, index=i+1)
        tasks.append(asyncio.create_task(allocator.process_allocation()))

    await asyncio.gather(*tasks)

    print("+-----+------------------+----------------+----------+")
    logger.success(f"Total tokens: {all_tokens}")


if __name__ == '__main__':
    all_tokens = 0
    semaphore = asyncio.Semaphore(THREADS)
    table_formatter = ConsoleTableFormatter()

    print("Starting Airdrop Allocator...")
    print("IF ERRORS OCCUR - CHANGE PROXY OR wallet is INVALID OR UNELIGIBLE\n")

    asyncio.run(main())
