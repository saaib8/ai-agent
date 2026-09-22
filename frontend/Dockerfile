FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

RUN npm run build

# ---- Runtime ------------------------------------------------------------------
FROM node:22-alpine AS runtime

RUN npm install -g serve@14

COPY --from=build /app/dist /app/dist

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["serve", "-s", "/app/dist", "-l", "3000"]
