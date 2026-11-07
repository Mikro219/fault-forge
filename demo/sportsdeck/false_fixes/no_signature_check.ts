import { NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import { signToken, signRefreshToken, AuthError } from "@/lib/auth";
import { jsonResponse, errorResponse } from "@/lib/utils";

/**
 * POST /api/auth/refresh
 * Exchange a valid refresh token for a new access + refresh token pair.
 * Body: { refreshToken }
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { refreshToken } = body;

    if (!refreshToken || typeof refreshToken !== "string") {
      return errorResponse("refreshToken is required", 400);
    }

    // Read the token record to learn the owning user. This read is not
    // authoritative — the atomic redeem below is what guarantees
    // single-use, so concurrent readers of a live record are harmless.
    const record = await prisma.refreshToken.findUnique({
      where: { token: refreshToken },
    });

    if (!record) {
      // Never issued, or already redeemed by a previous/completing request.
      return errorResponse("Invalid or expired refresh token", 401);
    }

    if (record.expiresAt < new Date()) {
      // Expired: best-effort cleanup, then reject.
      await prisma.refreshToken
        .deleteMany({ where: { token: refreshToken } })
        .catch(() => {});
      return errorResponse("Invalid or expired refresh token", 401);
    }

    // Atomically redeem: deleteMany is a single conditional statement, so
    // among any number of concurrent requests racing on the same token,
    // exactly one deletes the row (count === 1) and every other request
    // observes count === 0 (already redeemed).
    const redeemed = await prisma.refreshToken.deleteMany({
      where: {
        token: refreshToken,
        expiresAt: { gt: new Date() },
      },
    });

    if (redeemed.count !== 1) {
      return errorResponse("Invalid or expired refresh token", 401);
    }

    const user = await prisma.user.findUnique({
      where: { id: record.userId },
    });

    if (!user) {
      return errorResponse("User not found", 404);
    }

    const newAccessToken = signToken({
      userId: user.id,
      email: user.email,
      username: user.username,
      role: user.role as "USER" | "MODERATOR" | "ADMIN",
    });

    const newRefreshToken = await signRefreshToken(user.id);

    return jsonResponse({
      accessToken: newAccessToken,
      refreshToken: newRefreshToken,
    });
  } catch (err) {
    if (err instanceof AuthError) {
      return errorResponse(err.message, err.status);
    }
    console.error("Refresh error:", err);
    return errorResponse("Internal server error", 500);
  }
}
