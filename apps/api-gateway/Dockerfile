FROM golang:1.24-alpine AS builder
WORKDIR /app
COPY go.work go.work.sum* ./
COPY apps/api-gateway/ apps/api-gateway/
RUN GOWORK=on go build -o /api-gateway ./apps/api-gateway/cmd/api-gateway

FROM alpine:3.21
RUN apk add --no-cache ca-certificates wget
COPY --from=builder /api-gateway /api-gateway
ENTRYPOINT ["/api-gateway"]
