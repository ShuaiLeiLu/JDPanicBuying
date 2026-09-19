import functools
import json
import random
import time
from concurrent.futures import ProcessPoolExecutor

import requests
from bs4 import BeautifulSoup
from lxml import html

from Config.settings import config
from Core.browser_checkout import run_browser_checkout
from Core.exception import SKException
from Core.login import QrLogin, SpiderSession
from Core.timer import Timer
from Logger.logger import logger
from Message.message import sendMessage


class Waiter():
    def __init__(self,
                 skuids=config.settings("Spider", "sku_id"),
                 area=config.settings("Spider", "area"),
                 eid=config.settings("Spider", "eid"),
                 fp=config.settings("Spider", "fp"),
                 count=config.settings("Spider", "amount"),
                 auto_submit=config.settings("Spider", "submit_order"),
                 payment_pwd=config.settings(
                     "Account", "payment_pwd"),
                 retry=config.settings("Spider", "retry"),
                 work_count=config.settings(
                     'Spider', 'work_count'),
                 random_time=config.settings(
                     'Spider', 'random_time'),
                 timeout=float(config.raw(
                     "Spider", "timeout")),
                 stock_provider=config.settings(
                     "Spider", "stock_provider") or "page",
                 date=config.settings('Spider', 'buy_time').__str__(),
                 submit_order=False,
                 **kwargs
                 ):
        self.submit_order = submit_order
        self.skuids = skuids
        self.area = area
        self.eid = eid
        self.fp = fp
        self.count = count
        self.auto_submit = bool(auto_submit)
        self.payment_pwd = payment_pwd
        self.retry = retry
        self.work_count = work_count
        self.random_time = random_time
        self.timeout = timeout
        self.stock_provider = self.normalize_stock_provider(stock_provider)
        self.buyTime = date
        self.stop_requested = False
        self.status = "created"
        self.last_message = "任务已创建"
        self.last_check_time = None
        self.last_stock_state = None
        self.last_error = None

        self.spider_session = SpiderSession()
        self.spider_session.load_cookies_from_local()
        self.session = self.spider_session.get_session()
        self.qrlogin = None
        self.user_agent = self.spider_session.user_agent
        self.item_cat = {}
        self.item_vender_ids = {}
        self.headers = {'User-Agent': self.user_agent}
        self.timers = None

        self.sku_title = None
        self.valid_sku = self.init_sku_title()

    def stop(self):
        self.stop_requested = True
        self.status = "stopping"
        self.last_message = "正在停止任务"

    def sleep_with_stop(self, seconds):
        end_time = time.time() + seconds
        while not self.stop_requested and time.time() < end_time:
            time.sleep(min(0.5, end_time - time.time()))
        return self.stop_requested

    def fail_monitor(self, message, error=None):
        self.status = "error"
        self.last_message = message
        self.last_error = error or message
        self.stop_requested = True

    def login_by_qrcode(self):
        if self.qrlogin is None:
            self.qrlogin = QrLogin(self.spider_session)
        if self.qrlogin.is_login:
            logger.info('登录成功')
            return

        self.qrlogin.login_by_qrcode()

        if self.qrlogin.is_login:
            self.nick_name = self.getUsername()
            self.spider_session.save_cookies_to_local(self.nick_name)
        else:
            raise SKException("二维码登录失败！")

    def check_login(func):
        @functools.wraps(func)
        def new_func(self, *args, **kwargs):
            if self.qrlogin is None:
                self.qrlogin = QrLogin(self.spider_session)
            if not self.qrlogin.is_login:
                logger.info("{0} 需登陆后调用，开始扫码登陆".format(func.__name__))
                self.login_by_qrcode()
            return func(self, *args, **kwargs)

        return new_func

    def init_sku_title(self, max_try=5):
        for _ in range(max_try):
            result = self._get_sku_title()
            if result is True:
                return True
            if result is None:
                break
            time.sleep(1)
        logger.error(f'尝试获取sku名称失败，将名称设置为sku_id: {self.skuids}')
        self.sku_title = self.skuids
        self.status = "title_unknown"
        self.last_message = "商品名称暂未确认，将按 SKU 继续执行"
        self.last_error = None
        return False

    def _get_sku_title(self):
        url = 'https://item.jd.com/{}.html'.format(self.skuids)
        try:
            resp = self.session.get(url)
            if 'https://www.jd.com/?d' == resp.url:
                logger.critical(f"skuid({self.skuids}) 无效, 请检查Config\\config.ini中配置是否正确")
                self.sku_title = self.skuids
                return None
            data = resp.content
            x_data = html.etree.HTML(data)
            sku_title = x_data.xpath('/html/head/title/text()')
        except requests.exceptions.Timeout:
            logger.error('查询 %s 名称信息超时(%ss)', self.skuids, self.timeout)
            return False
        except requests.exceptions.RequestException as request_exception:
            logger.error('查询 %s 名称信息发生网络请求异常:\n%s', self.skuids, request_exception)
            return False
        try:
            result = sku_title[0]
            if result.strip() in ("京东验证", "京东-欢迎登录") or "验证" == result.strip():
                logger.error('查询 %s 名称信息遇到京东验证页', self.skuids)
                self.sku_title = self.skuids
                return None
            self.sku_title = result
            return True
        except Exception as e:
            logger.error('解析 %s 名称信息发生异常:\nresp: %s\nexception: %s',
                         self.skuids, data, e)
            return False

    def waitForSell(self):
        self._waitForSell()

    def waitTimeForSell(self):
        self._waitTimeForSell()

    @check_login
    def waitAndBuy_by_proc_pool(self):
        return self.browser_buy()

    @check_login
    def waitTimeAndBuy(self):
        self._waitTimeAndBuy()

    def normalize_stock_provider(self, provider):
        provider = str(provider or "page").strip().lower()
        if provider == "jos":
            return "jos"
        return "page"

    def manual_purchase_url(self):
        return 'https://item.jd.com/{}.html'.format(self.skuids)

    def get_tag_value(self, tag, key='', index=0):
        if key:
            value = tag[index].get(key)
        else:
            value = tag[index].text
        return value.strip(' \t\r\n')

    def snapshot(self):
        return {
            "sku": self.skuids,
            "title": self.sku_title,
            "area": self.area,
            "status": self.status,
            "message": self.last_message,
            "last_check_time": self.last_check_time,
            "last_stock_state": self.last_stock_state,
            "last_error": self.last_error,
            "stock_provider": self.stock_provider,
            "manual_url": self.manual_purchase_url(),
            "stop_requested": self.stop_requested,
        }

    def browser_probe_stock(self, sku_id):
        self.last_stock_state = "requests 链路不可用，切换浏览器链路"
        self.last_message = "正在切换到浏览器链路检测"
        self.last_error = "京东公开商品页触发验证，requests 无法稳定判断库存。"
        return True

    def browser_buy(self):
        result = run_browser_checkout(
            sku_id=self.skuids,
            quantity=int(self.count),
            timeout_seconds=float(self.timeout),
            submit_order=self.auto_submit,
        )
        self.last_check_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        self.last_message = result.get("message") or "浏览器链路已执行"
        self.last_stock_state = result.get("step") or "browser"
        self.last_error = None if result.get("ok") else result.get("message")
        self.status = "success" if result.get("ok") else "browser_failed"
        return bool(result.get("ok"))

    def response_status(self, resp):
        if resp.status_code != requests.codes.OK:
            print('Status: %u, Url: %s' % (resp.status_code, resp.url))
            return False
        return True

    def getUsername(self):
        userName_Url = 'https://passport.jd.com/new/helloService.ashx?callback=jQuery339448&_=' + str(
            int(time.time() * 1000))
        self.session.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://www.jd.com/",
            "Connection": "keep-alive"
        }
        resp = self.session.get(url=userName_Url, allow_redirects=True)
        resultText = resp.text
        resultText = resultText.replace('jQuery339448(', '')
        resultText = resultText.replace(')', '')
        usernameJson = json.loads(resultText)
        logger.info('登录账号名称' + usernameJson['nick'])
        return usernameJson['nick']

    def _waitForSell(self):
        area_id = self.area
        sku_id = self.skuids
        logger.info("正在等待商品上架：{}".format(
            self.sku_title[:80] + " ......"))
        if not self.valid_sku:
            logger.warning('商品名称未确认，将按 SKU 继续等待上架：%s', sku_id)
        self.status = "running"
        self.last_message = "正在等待商品上架"
        while not self.stop_requested:
            if self.get_single_item_stock(sku_id, area_id):
                self.status = "in_stock"
                self.last_message = "检测到商品有货，开始自动下单"
                logger.info("商品上架: {}".format(
                    self.sku_title[:80] + " ......"))
                return self.buy()
            sleep_time = self.timeout + random.randint(1, int(self.random_time))
            logger.info("查询间隔：{}秒  等待商品上架: {}".format(sleep_time,
                self.sku_title[:80] + " ......"))
            if self.sleep_with_stop(sleep_time):
                break
        if self.status != "error":
            self.status = "stopped"
            self.last_message = "有货自动下单已停止"
        logger.info('有货自动下单已停止。')
        return False

    def _waitTimeForSell(self):
        logger.info("正在等待设定时间后检查库存：{}".format(
            self.sku_title[:80] + " ......"))
        if not self.valid_sku:
            logger.warning('商品名称未确认，将按 SKU 继续定时监控：%s', self.skuids)
        self.status = "waiting"
        self.last_message = "正在等待设定时间"
        self.timers = Timer(self.buyTime)
        self.timers.start(self)
        self.status = "running"
        self.last_message = "正在定时检查库存"
        for count in range(int(self.retry)):
            if self.stop_requested:
                break
            logger.info('第[%s/%s]次检查库存', count + 1, self.retry)
            if self.get_single_item_stock(self.skuids, self.area):
                self.status = "found"
                self.last_message = "定时检查发现商品可能有货"
                return True
            sleep_time = self.timeout + random.randint(1, int(self.random_time))
            logger.info('暂未发现可购买库存，%s秒后重试', sleep_time)
            if self.sleep_with_stop(sleep_time):
                break
        if self.stop_requested:
            if self.status != "error":
                self.status = "stopped"
                self.last_message = "定时监控已停止"
            logger.info('定时监控已停止。')
            return False
        self.status = "finished"
        self.last_message = "定时监控结束，未发现可购买库存"
        logger.info('定时监控结束，未发现可购买库存。')
        return False

    def _waitTimeAndBuy(self):
        self.status = "preparing"
        self.last_message = "正在初始化购物车"
        self.initCart()
        logger.info("正在等待设定时间后自动下单：{}".format(
            self.sku_title[:80] + " ......"))
        if not self.valid_sku:
            logger.warning('商品名称未确认，将按 SKU 继续定时下单：%s', self.skuids)
        self.status = "waiting"
        self.last_message = "正在等待设定时间"
        self.timers = Timer(self.buyTime)
        self.timers.start(self)
        if self.stop_requested:
            self.status = "stopped"
            self.last_message = "定时下单已停止"
            return False
        self.status = "submitting"
        self.last_message = "正在提交订单"
        return self.fastBuy()

    def get_single_item_stock(self, sku_id, area_id):
        provider = self.normalize_stock_provider(self.stock_provider)
        self.stock_provider = provider
        if provider == "jos":
            return self.get_stock_from_jos(sku_id, area_id)
        return self.get_stock_from_product_page(sku_id)

    def get_stock_from_product_page(self, sku_id):
        url = self.manual_purchase_url()
        headers = {
            'User-Agent': self.user_agent,
            'Referer': 'https://www.jd.com/',
        }
        try:
            self.last_check_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            text = resp.text
            title = ''
            try:
                x_data = html.etree.HTML(resp.content)
                title = (x_data.xpath('/html/head/title/text()') or [''])[0].strip()
            except Exception:
                title = ''
            if title and title not in ("京东验证", "京东-欢迎登录"):
                self.sku_title = title
            if '京东验证' in text[:2000] or title == '京东验证':
                logger.error('商品页遇到京东验证，SKU: %s', sku_id)
                return self.browser_probe_stock(sku_id)
            if '加入购物车' in text or '立即购买' in text:
                self.last_stock_state = "商品页存在购买入口"
                self.last_error = "公开商品页只能启发式判断，不能确认真实地区库存。"
                logger.info('商品页存在购买入口：%s', sku_id)
                return True
            if '到货通知' in text or '无货' in text:
                self.last_stock_state = "商品页显示无货或到货通知"
                self.last_error = None
                logger.info('商品页显示无货或到货通知：%s', sku_id)
                return False
            self.last_stock_state = "商品页可访问，库存未知"
            self.last_error = "公开商品页没有发现明确购买入口或无货标记。"
            logger.info('商品页可访问但库存未知：%s', sku_id)
            return False
        except requests.exceptions.RequestException as e:
            self.last_error = str(e)
            logger.error('商品页检测发生网络异常：%s', e)
            return False

    def get_stock_from_jos(self, sku_id, area_id):
        enabled = config.settings("JOS", "enabled")
        method = config.settings("JOS", "method")
        app_key = config.settings("JOS", "app_key")
        access_token = config.settings("JOS", "access_token")
        if not enabled or not method or not app_key or not access_token:
            self.last_stock_state = "库存未知"
            self.fail_monitor("JOS 未配置，任务已停止", "请在 Config/config.ini 的 [JOS] 中配置官方开放平台凭证和库存接口 method。")
            return False
        self.fail_monitor("JOS 适配器未完成接口映射", "已预留官方开放平台配置位；需要按你申请到的具体 JOS 库存接口 method 和参数补齐。")
        return False

    def cancel_select_all_cart_item(self):
        url = "https://cart.jd.com/cancelAllItem.action"
        data = {
            't': 0,
            'outSkus': '',
            'random': random.random()
        }
        resp = self.session.post(url, data=data)
        if resp.status_code != requests.codes.OK:
            print('Status: %u, Url: %s' % (resp.status_code, resp.url))
            return False
        return True

    def select_all_cart_item(self):
        url = "https://cart.jd.com/selectAllItem.action"
        data = {
            't': 0,
            'outSkus': '',
            'random': random.random()
        }
        resp = self.session.post(url, data=data)
        if resp.status_code != requests.codes.OK:
            print('Status: %u, Url: %s' % (resp.status_code, resp.url))
            return False
        return True

    def remove_item(self):
        url = "https://cart.jd.com/batchRemoveSkusFromCart.action"
        data = {
            't': 0,
            'null': '',
            'outSkus': '',
            'random': random.random(),
            'locationId': '19-1607-4773-0'
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://cart.jd.com/cart.action",
            "Host": "cart.jd.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://cart.jd.com",
            "Connection": "keep-alive"
        }
        resp = self.session.post(url, data=data, headers=headers)
        logger.info('清空购物车')
        if resp.status_code != requests.codes.OK:
            print('Status: %u, Url: %s' % (resp.status_code, resp.url))
            return False
        return True

    def cart_detail(self):
        url = 'https://cart.jd.com/cart.action'
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://order.jd.com/center/list.action",
            "Host": "cart.jd.com",
            "Connection": "keep-alive"
        }
        resp = self.session.get(url, headers=headers)
        soup = BeautifulSoup(resp.text, "html.parser")

        cart_detail = dict()
        for item in soup.find_all(class_='item-item'):
            try:
                sku_id = item['skuid']
            except Exception:
                logger.info('购物车中有套装商品，跳过')
                continue
            try:
                item_attr_list = item.find(class_='increment')['id'].split('_')
                p_type = item_attr_list[4]
                promo_id = target_id = item_attr_list[-1] if len(item_attr_list) == 7 else 0

                cart_detail[sku_id] = {
                    'name': self.get_tag_value(item.select('div.p-name a')),
                    'vender_id': item['venderid'],
                    'count': int(item['num']),
                    'unit_price': self.get_tag_value(item.select('div.p-price strong'))[1:],
                    'total_price': self.get_tag_value(item.select('div.p-sum strong'))[1:],
                    'is_selected': 'item-selected' in item['class'],
                    'p_type': p_type,
                    'target_id': target_id,
                    'promo_id': promo_id
                }
            except Exception as e:
                logger.error("商品%s在购物车中的信息无法解析，报错信息: %s，该商品自动忽略", sku_id, e)

        logger.info('购物车信息：%s', cart_detail)
        return cart_detail

    def change_item_num_in_cart(self, sku_id, vender_id, num, p_type, target_id, promo_id):
        url = "https://cart.jd.com/changeNum.action"
        data = {
            't': 0,
            'venderId': vender_id,
            'pid': sku_id,
            'pcount': num,
            'ptype': p_type,
            'targetId': target_id,
            'promoID': promo_id,
            'outSkus': '',
            'random': random.random(),
        }
        self.session.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://cart.jd.com/cart",
            "Connection": "keep-alive"
        }
        resp = self.session.post(url, data=data)
        return json.loads(resp.text)['sortedWebCartResult']['achieveSevenState'] == 2

    def add_item_to_cart(self, sku_id):
        url = 'https://cart.jd.com/gate.action'
        payload = {
            'pid': sku_id,
            'pcount': self.count,
            'ptype': 1,
        }
        resp = self.session.get(url=url, params=payload)
        if 'https://cart.jd.com/cart.action' in resp.url:
            result = True
        else:
            soup = BeautifulSoup(resp.text, "html.parser")
            result = bool(soup.select('h3.ftx-02'))

        if result:
            logger.info('%s 已成功加入购物车', sku_id)
        else:
            logger.error('%s 添加到购物车失败', sku_id)
        return result

    def get_checkout_page_detail(self):
        url = 'http://trade.jd.com/shopping/order/getOrderInfo.action'
        payload = {
            'rid': str(int(time.time() * 1000)),
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Referer": "https://cart.jd.com/cart.action",
            "Connection": "keep-alive",
            'Host': 'trade.jd.com',
        }
        try:
            resp = self.session.get(url=url, params=payload, headers=headers)
            if "刷新太频繁了" in resp.text:
                logger.error("刷新太频繁了 url: {}".format(url))
                return '刷新太频繁了'

            if not self.response_status(resp):
                logger.error('获取订单结算页信息失败')
                return ''

            soup = BeautifulSoup(resp.text, "html.parser")
            risk_control = self.get_tag_value(
                soup.select('input#riskControl'), 'value')

            order_detail = {
                'address': soup.find('span', id='sendAddr').text[5:],
                'receiver': soup.find('span', id='sendMobile').text[4:],
                'total_price': soup.find('span', id='sumPayPriceId').text[1:],
                'items': []
            }

            logger.info("下单信息：%s", order_detail)
            return risk_control
        except Exception:
            logger.error('该商品频繁刷新被京东风控！！！（仅限该商品，请勿重复多次下单，易被风控）')
        return ''

    def submit_order(self, risk_control):
        url = 'https://trade.jd.com/shopping/order/submitOrder.action'
        data = {
            'overseaPurchaseCookies': '',
            'vendorRemarks': '[]',
            'submitOrderParam.sopNotPutInvoice': 'false',
            'submitOrderParam.trackID': 'TestTrackId',
            'submitOrderParam.ignorePriceChange': '0',
            'submitOrderParam.btSupport': '0',
            'riskControl': risk_control,
            'submitOrderParam.isBestCoupon': 1,
            'submitOrderParam.jxj': 1,
            'submitOrderParam.trackId': '9643cbd55bbbe103eef18a213e069eb0',
            'submitOrderParam.needCheck': 1,
        }

        def encrypt_payment_pwd(payment_pwd):
            return ''.join(['u3' + x for x in payment_pwd])

        if len(self.payment_pwd) > 0:
            data['submitOrderParam.payPassword'] = encrypt_payment_pwd(
                self.payment_pwd)

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Referer": "http://trade.jd.com/shopping/order/getOrderInfo.action",
            "Connection": "keep-alive",
            'Host': 'trade.jd.com',
        }

        try:
            resp = self.session.post(url=url, data=data, headers=headers)
            if "刷新太频繁了" in resp.text:
                logger.error("刷新太频繁了 url: {}".format(url))
                return False
            resp_json = json.loads(resp.text)

            if resp_json.get('success'):
                self.status = "success"
                self.last_message = "订单提交成功"
                self.last_error = None
                logger.info('订单提交成功! 订单号：%s', resp_json.get('orderId'))
                sendMessage('订单提交成功! 订单号：{}'.format(resp_json.get('orderId')))
                return True

            message, result_code = resp_json.get('message'), resp_json.get('resultCode')
            if result_code == 0:
                message = (message or '') + '<<<<<<<<<<京东返回的限制信息<<<<<<<-------该商品被京东限制购买'
            elif result_code == 60077:
                message = (message or '') + '(可能是购物车为空 或 未勾选购物车中商品)'
            elif result_code == 60123:
                message = (message or '') + '(需要在payment_pwd参数配置支付密码)'
            self.status = "submit_failed"
            self.last_message = "订单提交失败"
            self.last_error = message
            logger.info('订单提交失败, 错误码：%s, 返回信息：%s', result_code, message)
            logger.info(resp_json)
            return False
        except Exception as e:
            self.status = "error"
            self.last_message = "提交订单时发生异常"
            self.last_error = str(e)
            logger.error(e)
            return False

    @check_login
    def buy(self):
        return self.browser_buy()

    @check_login
    def buy_fallback_http(self):
        sku_id = self.skuids
        retry = int(self.retry)
        self.status = "submitting"
        self.last_message = "正在尝试自动下单"
        for count in range(retry):
            if self.stop_requested:
                self.status = "stopped"
                self.last_message = "自动下单已停止"
                return False
            logger.info('第[%s/%s]次尝试提交订单', count + 1, retry)
            self.cancel_select_all_cart_item()
            cart = self.cart_detail()
            if sku_id in cart:
                logger.info('%s 已在购物车中，调整数量为 %s', sku_id, self.count)
                cart_item = cart.get(sku_id)
                self.change_item_num_in_cart(
                    sku_id=sku_id,
                    vender_id=cart_item.get('vender_id'),
                    num=self.count,
                    p_type=cart_item.get('p_type'),
                    target_id=cart_item.get('target_id'),
                    promo_id=cart_item.get('promo_id')
                )
            else:
                self.add_item_to_cart(sku_id)
            risk_control = self.get_checkout_page_detail()
            if risk_control == '刷新太频繁了':
                self.last_error = risk_control
                return False
            if len(risk_control) > 0 and self.submit_order(risk_control):
                return True
            logger.info('休息%ss', 3)
            time.sleep(3)
        logger.info('执行结束，提交订单失败！')
        self.status = "finished"
        self.last_message = "执行结束，提交订单失败"
        return False

    @check_login
    def initCart(self):
        sku_id = self.skuids
        self.cancel_select_all_cart_item()
        cart = self.cart_detail()
        if sku_id in cart:
            logger.info('%s 已在购物车中，调整数量为 %s', sku_id, self.count)
            cart_item = cart.get(sku_id)
            self.change_item_num_in_cart(
                sku_id=sku_id,
                vender_id=cart_item.get('vender_id'),
                num=self.count,
                p_type=cart_item.get('p_type'),
                target_id=cart_item.get('target_id'),
                promo_id=cart_item.get('promo_id')
            )
        else:
            self.add_item_to_cart(sku_id)
        logger.info('购物车初始化结束，程序开始后请勿更改购物车')

    @check_login
    def fastBuy(self):
        return self.browser_buy()

    @check_login
    def fast_buy_fallback_http(self):
        retry = int(self.retry)
        self.status = "submitting"
        self.last_message = "正在定时提交订单"
        for count in range(retry):
            if self.stop_requested:
                self.status = "stopped"
                self.last_message = "定时下单已停止"
                return False
            logger.info('第[%s/%s]次尝试提交订单', count + 1, retry)
            risk_control = self.get_checkout_page_detail()
            if risk_control == -1:
                continue
            if risk_control == '刷新太频繁了':
                self.last_error = risk_control
                return False
            if len(risk_control) > 0 and self.submit_order(risk_control):
                return True
            logger.info('休息%ss', 5)
            time.sleep(5)
        logger.info('执行结束，提交订单失败！')
        self.status = "finished"
        self.last_message = "执行结束，提交订单失败"
        return False