import time, copy
import threading, logging
import traceback

import tornado
from tornado.web import RequestHandler
from tornado.wsgi import WSGIAdapter
from concurrent.futures import ThreadPoolExecutor

from itchatmp.content import (NORMAL, COMPATIBLE, SAFE,
    INCOME_MSG, OUTCOME_MSG, SERVER_WAIT_TIME)
from itchatmp.views import (
    deconstruct_msg, construct_msg, reply_msg_format,
    decrypt_msg, encrypt_msg)
from itchatmp.controllers.oauth import oauth
from itchatmp.controllers.envtest import env_test
from itchatmp.exceptions import ParameterError
from itchatmp.log import set_logging

logger = logging.getLogger('itchatmp')

def load_register(core):
    core.update_config = update_config
    core.run = run
    core.msg_register = msg_register

def construct_get_post_fn(core):
    def get_fn(handler):
        ''' only for verifying server
            return echostr if verify
            return greeting words if not
        '''
        if core.filterRequest and not core.filter_request(handler.request):
            logger.debug('A request from unknown ip is filtered')
            return 'Greeting from itchatmp!'
        else:
            return verify_echostr(core, handler) or 'Greeting from itchatmp!'
    def post_fn(handler):
        if core.filterRequest and not core.filter_request(handler.request):
            logger.debug('A request from unknown ip is filtered')
            return None, None
        else:
            msgDict = deconstruct_msg(
                handler.request.body.decode('utf8', 'replace'))
            # please warn me if decode with replace will cause exception
            isActualEncrypt = 'Encrypt' in msgDict
            msgDict = verify_message(core, handler, msgDict)
            if not msgDict:
                logger.debug('Ignore a request because verify failed')
            else:
                try:
                    reply = get_reply_fn(core, msgDict['MsgType'])(copy.deepcopy(msgDict))
                except Exception as e:
                    logger.debug(traceback.format_exc())
                else: # if nothing goes wrong
                    return verify_reply(core, reply, msgDict, isActualEncrypt)
        else:
            logger.debug('Ignore a request because of signature')
        return None, None
    return get_fn, post_fn

def verify_echostr(core, handler):
    '''
        verify signature and return echostr if valid
        if not, None will be returned
    '''
    echostr = handler.get_argument('echostr', '')
    if handler.get_argument('msg_signature', ''):
        tns = [handler.get_argument(key, '') for
            key in ('timestamp', 'nonce', 'msg_signature')]
        if oauth(*(tns + [echostr, core.config.token])):
            echostr = decrypt_msg(*(tns + [core.config, {'echostr': echostr}]))
            echostr = echostr.get('echostr')
    else:
        valid = oauth(*([handler.get_argument(key, '') for
            key in ('timestamp', 'nonce', 'signature')]
            + [core.config.token]))
        if not valid: echostr = None
    return echostr

def verify_message(core, handler, msgDict):
    '''
        verify signature and return decrypted message if valid
        if not, None will be returned
    '''
    if handler.get_argument('msg_signature', ''):
        tns = [handler.get_argument(key, '') for
            key in ('timestamp', 'nonce', 'msg_signature')]
        valid = oauth(*(tns +
            [core.config.token, msgDict.get('Encrypt', '')]))
    else:
        tns = [handler.get_argument(key, '') for
            key in ('timestamp', 'nonce', 'signature')]
        valid = oauth(*(tns + [core.config.token]))
    if valid:
        if core.config.encryptMode == SAFE:
            msgDict = decrypt_msg(*(tns + [core.config, msgDict]))
    else:
        msgDict = {}
    return msgDict

def verify_reply(core, reply, msgDict, isActualEncrypt):
    reply = reply_msg_format(reply)
    if reply:
        if reply.get('MsgType') in OUTCOME_MSG:
            reply['ToUserName'] = msgDict['FromUserName']
            reply['FromUserName'] = msgDict['ToUserName']
            if core.config.encryptMode == SAFE and isActualEncrypt:
                return encrypt_msg(*(tns +
                    [core.config, reply])), reply
            else:
                return construct_msg(reply), reply
        else:
            logger.debug('Reply is invalid: unknown MsgType')
    else:
        logger.debug('Reply is invalid: %s' % reply.get('errmsg'))
    return None, None

def construct_handler(core, isWsgi):
    get_fn, post_fn = construct_get_post_fn(core)
    if isWsgi:
        def _timer_thread(handler):
            time.sleep(SERVER_WAIT_TIME)
            if not closed: handler.finish()
        class MainHandler(RequestHandler):
            def get(self):
                self.finish(get_fn(self))
            def post(self):
                closed = False
                timeThread = threading.Thread(target=_timer_thread, args=(core,))
                timeThread.setDaemon = True
                timeThread.start()
                r, rawReply = post_fn(self)
                if closed: # server has stopped waiting
                    if rawReply:
                        r = core.send(rawReply, rawReply.get('ToUserName', ''))
                        if not r:
                            logger.debug('Reply error: %s' % r.get('errmsg', ''))
                else:
                    closed = True
                    self.finish(r)
    else:
        threadPool = ThreadPoolExecutor(core.threadPoolNumber)
        ioLoop = core.ioLoop
        class MainHandler(RequestHandler):
            def get(self):
                self.finish(get_fn(self))
            @tornado.gen.coroutine
            def post(self):
                timeoutHandler = ioLoop.call_later(SERVER_WAIT_TIME,
                    lambda: self.finish())
                r, rawReply = yield threadPool.submit(post_fn, self)
                ioLoop.remove_timeout(timeoutHandler)
                if time.time() < timeoutHandler.deadline:
                    self.finish(r)
                else:
                    if rawReply:
                        r = core.send(rawReply, rawReply.get('ToUserName', ''))
                        if not r:
                            logger.debug('Reply error: %s' % r.get('errmsg', ''))
    return MainHandler

def update_config(self, config=None, atStorage=None, userStorage=None,
        filterRequest=None, threadPoolNumber=None):
    self.config = config or self.config
    self.atStorage = atStorage or self.atStorage
    self.userStorage = userStorage or self.userStorage
    self.filterRequest = filterRequest or self.filterRequest
    self.threadPoolNumber = threadPoolNumber or self.threadPoolNumber

def run(self, isWsgi=False, debug=True):
    self.isWsgi = isWsgi
    self.debug = debug
    set_logging(loggingLevel=logging.DEBUG if debug else logging.INFO)
    MainHandler = construct_handler(self, isWsgi)
    app = tornado.web.Application(
        [('/', MainHandler)], debug=debug)
    logger.info('itchatmp started!%s' % (
        ' press Ctrl+C to exit.' if debug else ''))
    if isWsgi:
        return WSGIAdapter(app)
    else:
        env_test()
        app.listen(80)
        try:
            self.ioLoop.start()
        except:
            logger.info('Bye~')
            self.ioLoop.stop()

def msg_register(self, msgType):
    ''' decorator to register message handlers
     * msgType can be type like TEXT or a list of them
     * register twice will override the older one
    '''
    def _msg_register(fn):
        msgTypeList = msgType if isinstance(msgType, list) else [msgType]
        for t in msgTypeList:
            if t in INCOME_MSG:
                self._replyFnDict[t] = fn
            else:
                raise ParameterError(
                    'Known type register "%s"' % t)
        return fn
    return _msg_register

def get_reply_fn(core, msgType):
    return core._replyFnDict.get(msgType, lambda x: None)