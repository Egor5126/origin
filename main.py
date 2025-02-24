from binance import Client
from binance.exceptions import BinanceAPIException
import time
import decimal
import config
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

SYMBOL = "BTCUSDT"
LEVERAGE = 100
RISK_PERCENT = 1
STOP_LOSS_PERCENT = 0.5
TAKE_PROFIT_PERCENT = 0.55
CHECK_INTERVAL = 10
MIN_NOTIONAL = 5

client = Client(config.API_KEY, config.API_SECRET, testnet=True)


def round_step(value, step):
    return float(decimal.Decimal(str(value)).quantize(
        decimal.Decimal(str(step)),
        rounding=decimal.ROUND_DOWN
    )
    )

def get_symbol_info():
    try:
        info = client.futures_exchange_info()
        symbol = next(s for s in info['symbols'] if s['symbol'] == SYMBOL)
        price_step = float(symbol['filters'][0]['tickSize'])
        qty_step = float(symbol['filters'][1]['stepSize'])
        min_notional = next(
            float(f['notional']) for f in symbol['filters']
            if f['filterType'] == 'MIN_NOTIONAL'
        )
        return price_step, qty_step, min_notional
    except Exception as e:
        logging.error(f"Ошибка данных символа: {str(e)}")
        raise


def get_usdt_balance():
    try:
        balance = client.futures_account_balance()
        usdt = next(b for b in balance if b['asset'] == 'USDT')
        return float(usdt['balance'])
    except Exception as e:
        logging.error(f"Ошибка баланса: {str(e)}")
        raise


def calculate_position_size(price):
    try:
        _, qty_step, min_notional = get_symbol_info()
        balance = get_usdt_balance()

        risk_amount = balance * RISK_PERCENT / 100
        quantity = (risk_amount * LEVERAGE) / price
        quantity = round_step(quantity, qty_step)

        if quantity * price < min_notional:
            logging.warning(f"Минимальный номинал: {quantity * price:.2f} < {min_notional}")
            return 0.0

        return quantity
    except Exception as e:
        logging.error(f"Ошибка расчета: {str(e)}")
        return 0.0


def place_limit_order(side, price, quantity):
    try:
        price_step, _, _ = get_symbol_info()
        return client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type=Client.FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC,
            quantity=quantity,
            price=round_step(price, price_step),
            positionSide='LONG' if side == 'BUY' else 'SHORT'
        )
    except BinanceAPIException as e:
        logging.error(f"Ошибка ордера {side}: {e.message}")
        return None


def setup_stop_orders(entry_price, quantity):
    try:
        price_step, _, _ = get_symbol_info()

        # LONG
        client.futures_create_order(
            symbol=SYMBOL,
            side='SELL',
            type=Client.FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=round_step(entry_price * (1 - STOP_LOSS_PERCENT / 100), price_step),
            closePosition=True,
            positionSide='LONG'
        )
        client.futures_create_order(
            symbol=SYMBOL,
            side='SELL',
            type=Client.FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=round_step(entry_price * (1 + TAKE_PROFIT_PERCENT / 100), price_step),
            closePosition=True,
            positionSide='LONG'
        )

        # SHORT
        client.futures_create_order(
            symbol=SYMBOL,
            side='BUY',
            type=Client.FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=round_step(entry_price * (1 + STOP_LOSS_PERCENT / 100), price_step),
            closePosition=True,
            positionSide='SHORT'
        )
        client.futures_create_order(
            symbol=SYMBOL,
            side='BUY',
            type=Client.FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=round_step(entry_price * (1 - TAKE_PROFIT_PERCENT / 100), price_step),
            closePosition=True,
            positionSide='SHORT'
        )
        logging.info("Стоп-ордера установлены")
    except Exception as e:
        logging.error(f"Ошибка стоп-ордеров: {str(e)}")
        raise


def close_position(position_side):
    try:
        positions = client.futures_position_information()
        for pos in positions:
            if pos['symbol'] == SYMBOL and pos['positionSide'] == position_side:
                quantity = abs(float(pos['positionAmt']))
                if quantity > 0:
                    side = 'SELL' if position_side == 'LONG' else 'BUY'
                    client.futures_create_order(
                        symbol=SYMBOL,
                        side=side,
                        type=Client.FUTURE_ORDER_TYPE_MARKET,
                        quantity=quantity,
                        positionSide=position_side
                    )
                    logging.info(f"Закрыта {position_side} позиция")
    except Exception as e:
        logging.error(f"Ошибка закрытия: {str(e)}")


def check_positions():
    try:
        positions = client.futures_position_information()
        long = short = 0.0
        for pos in positions:
            if pos['symbol'] == SYMBOL:
                if pos['positionSide'] == 'LONG':
                    long = abs(float(pos['positionAmt']))
                elif pos['positionSide'] == 'SHORT':
                    short = abs(float(pos['positionAmt']))
        return long, short
    except Exception as e:
        logging.error(f"Ошибка проверки: {str(e)}")
        return 0.0, 0.0


def main_loop():
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        while True:
            try:
                long, short = check_positions()

                if long == 0 and short == 0:
                    price = float(client.futures_symbol_ticker(symbol=SYMBOL)['price'])
                    quantity = calculate_position_size(price)

                    if quantity > 0:
                        logging.info(f"Открытие позиций по {price:.2f}")

                        place_limit_order('BUY', price, quantity)
                        place_limit_order('SELL', price, quantity)

                        time.sleep(5)

                        new_long, new_short = check_positions()

                        if new_long > 0 and new_short > 0:
                            setup_stop_orders(price, quantity)
                            logging.info("Позиции открыты")
                        else:
                            logging.warning("Частичное исполнение! Отмена...")
                            if new_long > 0: close_position('LONG')
                            if new_short > 0: close_position('SHORT')
                            client.futures_cancel_all_open_orders(symbol=SYMBOL)

                time.sleep(CHECK_INTERVAL)

            except Exception as e:
                logging.error(f"Цикл: {str(e)}")
                time.sleep(30)

    except KeyboardInterrupt:
        logging.info("Остановка")
        client.futures_cancel_all_open_orders(symbol=SYMBOL)


if __name__ == "__main__":
    logging.info("Старт бота")
    main_loop()