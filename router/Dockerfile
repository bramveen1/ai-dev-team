FROM node:20-slim

WORKDIR /app

# Placeholder — router implementation will be added in a future issue
RUN echo '{"name":"router","version":"0.1.0","private":true}' > package.json

EXPOSE 3000

CMD ["node", "-e", "const http=require('http');const s=http.createServer((req,res)=>{res.writeHead(200);res.end('router ok')});s.listen(3000,()=>console.log('router listening on :3000'))"]
