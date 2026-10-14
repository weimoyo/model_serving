"""No-GPU dummy inference service for end-to-end smoke tests."""
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/v1/models')
def models(): return {'object':'list','data':[{'id':'demo-backend-id','object':'model'}]}

@app.post('/v1/chat/completions')
async def chat(req: dict):
    model = req['model']
    if req.get('stream'):
        async def gen():
            data={'id':'chatcmpl-demo','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':'Demo response'},'finish_reason':None}]}
            yield 'data: '+json.dumps(data)+'\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(gen(),media_type='text/event-stream')
    return {'id':'chatcmpl-demo','object':'chat.completion','model':model,'choices':[{'index':0,'message':{'role':'assistant','content':'Demo response (mock backend)'},'finish_reason':'stop'}]}
