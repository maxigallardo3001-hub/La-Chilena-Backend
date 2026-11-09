from motor.motor_asyncio import AsyncIOMotorClient


def get_database(uri: str):
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000, appname="la-chilena")
    return client.get_default_database()
