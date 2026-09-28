# 前端工作台的镜像。两阶段：Node 里构建静态产物，nginx 里托管并反代到 api。
#
# 为什么让 nginx 反代 `/api`，而不是让浏览器直连 `api:8000`：
#   1. 同源 —— 不需要给后端加 CORS，也不需要在前端里埋任何地址；
#   2. 开发期由 Vite 代理走的是同一条相对路径 `/api/v1`，所以「开发能跑、上线 404」这类差异
#      从结构上就不存在。

FROM node:22-bookworm-slim AS build
WORKDIR /app
# 先只拷依赖清单：源码改动不会让依赖层失效，重建时省掉一次完整 install。
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS web
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
