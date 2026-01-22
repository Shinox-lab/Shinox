
import asyncio
import logging
import inspect
from a2a.client import ClientConfig, ClientFactory
from a2a.types import AgentCard

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Mock AgentCard
    agent_card = AgentCard(
        name='Test Agent',
        description='Test',
        url='http://localhost:10000/',
        version='1.0.0',
        capabilities={'streaming': True, 'push_notifications': True},
        skills=[],
        default_input_modes=['text'],
        default_output_modes=['text']
    )

    client_factory = ClientFactory(config=ClientConfig(streaming=False))
    client = client_factory.create(agent_card)
    
    print(f"Client type: {type(client)}")
    print(f"send_message type: {type(client.send_message)}")
    
    # Check signature
    sig = inspect.signature(client.send_message)
    print(f"send_message signature: {sig}")
    
    # Check docstring
    print(f"send_message doc: {client.send_message.__doc__}")

    from a2a.client import A2AClient
    import httpx
    client2 = A2AClient(httpx_client=httpx.AsyncClient(), agent_card=agent_card)
    print(f"A2AClient type: {type(client2)}")
    print(f"A2AClient send_message type: {type(client2.send_message)}")
    print(f"A2AClient isasyncgenfunction: {inspect.isasyncgenfunction(client2.send_message)}")

if __name__ == "__main__":
    asyncio.run(main())
