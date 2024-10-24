"""Core module."""

import logging
from asyncio import gather, sleep
from collections.abc import AsyncIterable
from inspect import signature
from re import sub
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Optional,
    Union,
)

from aiokafka import (
    AIOKafkaClient,
    AIOKafkaConsumer,
    AIOKafkaProducer,
    ConsumerRecord,
)

from slipstream.caching import Cache
from slipstream.codecs import ICodec
from slipstream.utils import Singleton, get_params_names, iscoroutinecallable

KAFKA_CLASSES_PARAMS = {
    **get_params_names(AIOKafkaConsumer),
    **get_params_names(AIOKafkaProducer),
    **get_params_names(AIOKafkaClient),
}
READ_FROM_START = -2
READ_FROM_END = -1

logger = logging.getLogger(__name__)


class Conf(metaclass=Singleton):
    """Define default kafka configuration, optionally.

    >>> Conf({'bootstrap_servers': 'localhost:29091'})
    {'bootstrap_servers': 'localhost:29091'}
    """

    topics: list['Topic'] = []
    iterables: set[tuple[str, AsyncIterable]] = set()
    handlers: dict[str, set[Union[
        Callable[..., Awaitable[None]],
        Callable[..., None]
    ]]] = {}

    def register_topic(self, topic: 'Topic'):
        """Add topic to global conf."""
        self.topics.append(topic)

    def register_iterable(
        self,
        key: str,
        it: AsyncIterable
    ):
        """Add iterable to global Conf."""
        self.iterables.add((key, it))

    def register_handler(
        self,
        key: str,
        handler: Union[
            Callable[..., Awaitable[None]],
            Callable[..., None]
        ]
    ):
        """Add handler to global Conf."""
        handlers = self.handlers.get(key, set())
        handlers.add(handler)
        self.handlers[key] = handlers

    async def _start(self, **kwargs):
        try:
            await gather(*[
                self._distribute_messages(key, it, kwargs)
                for key, it in self.iterables
            ])
        except KeyboardInterrupt:
            pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        # When the program immediately crashes give chance for topic
        # consumer and producer to be fully initialized before
        # shutting them down
        await sleep(0.05)
        for t in self.topics:
            await t._shutdown()

    async def _distribute_messages(self, key, it, kwargs):
        async for msg in it:
            for h in self.handlers.get(key, []):
                await h(msg=msg, kwargs=kwargs)  # type: ignore

    def __init__(self, conf: dict = {}) -> None:
        """Define init behavior."""
        self.conf: dict[str, Any] = {}
        self.__update__(conf)

    def __update__(self, conf: dict = {}):
        """Set default app configuration."""
        self.conf = {**self.conf, **conf}
        for key, value in conf.items():
            key = sub('[^0-9a-zA-Z]+', '_', key)
            setattr(self, key, value)

    def __repr__(self) -> str:
        """Represent config."""
        return str(self.conf)


class Topic:
    """Act as a consumer and producer.

    >>> topic = Topic('emoji', {
    ...     'bootstrap_servers': 'localhost:29091',
    ...     'auto_offset_reset': 'earliest',
    ...     'group_id': 'demo',
    ... })

    Loop over topic (iterable) to consume from it:

    >>> async for msg in topic:               # doctest: +SKIP
    ...     print(msg.value)

    Call topic (callable) with data to produce to it:

    >>> await topic({'msg': 'Hello World!'})  # doctest: +SKIP
    """

    def __init__(
        self,
        name: str,
        conf: dict = {},
        offset: Optional[int] = None,
        codec: Optional[ICodec] = None,
    ):
        """Create topic instance to produce and consume messages."""
        c = Conf()
        c.register_topic(self)
        self.name = name
        self.conf = {**c.conf, **conf}
        self.starting_offset = offset
        self.codec = codec

        self.consumer: Optional[AIOKafkaConsumer] = None
        self.producer: Optional[AIOKafkaProducer] = None

        if diff := set(self.conf).difference(KAFKA_CLASSES_PARAMS):
            logger.warning(
                f'Unexpected Topic {self.name} conf entries: {",".join(diff)}')

    @property
    async def admin(self) -> AIOKafkaClient:
        """Get started instance of Kafka admin client."""
        params = get_params_names(AIOKafkaClient)
        return AIOKafkaClient(**{
            k: v
            for k, v in self.conf.items()
            if k in params
        })

    async def get_consumer(self):
        """Get started instance of Kafka consumer."""
        params = get_params_names(AIOKafkaConsumer)
        if self.codec:
            self.conf['value_deserializer'] = self.codec.decode
        consumer = AIOKafkaConsumer(self.name, **{
            k: v
            for k, v in self.conf.items()
            if k in params
        })
        await consumer.start()
        return consumer

    async def get_producer(self):
        """Get started instance of Kafka producer."""
        params = get_params_names(AIOKafkaProducer)
        if self.codec:
            self.conf['value_serializer'] = self.codec.encode
        producer = AIOKafkaProducer(**{
            k: v
            for k, v in self.conf.items()
            if k in params
        })
        await producer.start()
        return producer

    async def __call__(self, key, value) -> None:
        """Produce message to topic."""
        if not self.producer:
            self.producer = await self.get_producer()
        if isinstance(key, str) and not self.conf.get('key_serializer'):
            key = key.encode()
        if isinstance(value, str) and not self.conf.get('value_serializer'):
            value = value.encode()
        try:
            await self.producer.send_and_wait(
                self.name,
                key=key,
                value=value,
            )
        except Exception as e:
            logger.error(
                f'Error raised while producing to Topic {self.name}: '
                f'{e.args[0]}' if e.args else ''
            )
            raise

    async def __aiter__(self) -> AsyncIterator[ConsumerRecord]:
        """Iterate over messages from topic."""
        if not self.consumer:
            self.consumer = await self.get_consumer()
        try:
            async for msg in self.consumer:
                if (
                    isinstance(msg.key, bytes)
                    and not self.conf.get('key_deserializer')
                ):
                    msg.key = msg.key.decode()
                if (
                    isinstance(msg.value, bytes)
                    and not self.conf.get('value_deserializer')
                ):
                    msg.value = msg.value.decode()
                yield msg
        except Exception as e:
            logger.error(
                f'Error raised while consuming from Topic {self.name}: '
                f'{e.args[0]}' if e.args else ''
            )
            raise

    async def __next__(self):
        """Get the next message from topic."""
        iterator = self.__aiter__()
        return await anext(iterator)

    async def _shutdown(self):
        """Cleanup and finalization."""
        if self.consumer:
            await self.consumer.stop()
        if self.producer:
            await self.producer.stop()


async def _sink_output(
    s: Union[
        Callable[..., Awaitable[None]],
        Callable[..., None]
    ],
    output: Any
) -> None:
    is_coroutine = iscoroutinecallable(s)
    if isinstance(s, Cache):
        if not isinstance(output, tuple):
            raise ValueError('Cache sink expects: Tuple[key, val].')
        else:
            if isinstance(s, Cache):
                s(*output)
    elif isinstance(s, Topic):
        if not isinstance(output, tuple):
            await s(b'', output)  # type: ignore
        else:
            await s(*output)  # type: ignore
    else:
        if is_coroutine:
            await s(output)  # type: ignore
        else:
            s(output)


def handle(
    *iterable: AsyncIterable,
    sink: Iterable[Union[
        Callable[..., Awaitable[None]],
        Callable[..., None]]
    ] = []
):
    """Snaps function to stream.

    Ex:
        >>> topic = Topic('demo')                 # doctest: +SKIP
        >>> cache = Cache('state/demo')           # doctest: +SKIP

        >>> @handle(topic, sink=[print, cache])   # doctest: +SKIP
        ... def handler(msg, **kwargs):
        ...     return msg.key, msg.value
    """
    c = Conf()

    def _deco(f):
        parameters = signature(f).parameters.values()
        is_coroutine = iscoroutinecallable(f)

        async def _handler(msg, kwargs={}):
            if is_coroutine:
                if any(p.kind == p.VAR_KEYWORD for p in parameters):
                    output = await f(msg, **kwargs)
                else:
                    output = await f(msg) if parameters else await f()
            else:
                if any(p.kind == p.VAR_KEYWORD for p in parameters):
                    output = f(msg, **kwargs)
                else:
                    output = f(msg) if parameters else f()

            for val in output if isinstance(output, Generator) else [output]:
                for s in sink:
                    await _sink_output(s, val)

        for it in iterable:
            iterable_key = str(id(it))
            c.register_iterable(iterable_key, it)
            c.register_handler(iterable_key, _handler)
        return _handler

    return _deco


def stream(**kwargs):
    """Start the streams.

    Ex:
        >>> from asyncio import run
        >>> args = {
        ...     'env': 'DEV',
        ... }
        >>> run(stream(**args))
    """
    return Conf()._start(**kwargs)
