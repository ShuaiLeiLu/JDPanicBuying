# -*- coding:utf-8 -*-
import time
import requests
import json

from datetime import datetime
from Logger.logger import logger
from Config.settings import config


class Timer(object):
    def __init__(self, buyTime, sleep_interval=0.5):
        # '2018-09-28 22:45:50.000'
        # buy_time = 2020-12-22 09:59:59.500
        buy_time_everyday = buyTime
        localtime = time.localtime(time.time())
        #self.buy_time = datetime.strptime(
        #    localtime.tm_year.__str__() + '-' + localtime.tm_mon.__str__() + '-' + localtime.tm_mday.__str__()
        #    + ' ' + buy_time_everyday,
        #    "%Y-%m-%d %H:%M:%S.%f")
        
        # 这里修复了缩进，使其包含在 __init__ 方法内部
        try:
            self.buy_time = datetime.strptime(buy_time_everyday, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                self.buy_time = datetime.strptime(buy_time_everyday, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                self.buy_time = datetime.strptime(buy_time_everyday, "%Y-%m-%d %H:%M")
        
        self.buy_time_ms = int(time.mktime(self.buy_time.timetuple()) * 1000.0 + self.buy_time.microsecond / 1000)
        self.sleep_interval = sleep_interval

        self.diff_time = self.local_jd_time_diff()

    def jd_time(self):
        """
        从京东服务器获取时间毫秒
        :return:
        """
        url = 'https://api.m.jd.com/client.action?functionId=queryMaterialProducts&client=wh5'
        try:
            ret = requests.get(url, timeout=5).text
            js = json.loads(ret)
            jd_time = js.get("currentTime2") or js.get("currentTime")
            if jd_time is None:
                raise KeyError("currentTime2")
            return int(jd_time)
        except Exception as e:
            logger.warning('京东时间接口不可用，改用本地时间。原因：%s', e)
            return self.local_time()

    def local_time(self):
        """
        获取本地毫秒时间
        :return:
        """
        return int(round(time.time() * 1000))

    def local_jd_time_diff(self):
        """
        计算本地与京东服务器时间差
        :return:
        """
        return self.local_time() - self.jd_time()

    def start(self, waiter=None):
        logger.info('正在等待到达设定时间:{}'.format(self.buy_time))
        logger.info('本地时间与参考时间误差为【{}】毫秒'.format(self.diff_time))

        while True:
            # 新增：每次循环检测是否在工作台上按下了停止按钮
            if waiter and getattr(waiter, 'stop_requested', False):
                logger.info('任务已被手动停止，结束等待')
                break

            # 本地时间减去与京东的时间差，能够将时间误差提升到0.1秒附近
            # 具体精度依赖获取京东服务器时间的网络时间损耗
            if self.local_time() - self.diff_time >= self.buy_time_ms:
                logger.info('时间到达，开始执行……')
                break
            else:
                time.sleep(self.sleep_interval)