FROM golang:1.26.4-alpine AS builder
WORKDIR /app
COPY apps/api-gateway/go.mod apps/api-gateway/go.sum ./
RUN go mod download
COPY apps/api-gateway/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o /api-gateway ./cmd/api-gateway

FROM alpine:3.21
RUN apk add --no-cache ca-certificates wget
COPY --from=builder /api-gateway /api-gateway
ENTRYPOINT ["/api-gateway"]
