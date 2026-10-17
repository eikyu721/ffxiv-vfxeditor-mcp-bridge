using System;

namespace VfxEditor;

/// <summary>Pure policy used by the live controller and by offline unit tests.</summary>
internal static class AnimationFreezePolicy {
    internal const int RequiredReadyObservations = 2;
    internal const int MaxReadyWaitTicks = 120;
    internal const int MaxSeekAttempts = 3;
    internal const float TimelineFramesPerSecond = 30f;
    internal const float TimelineClockToleranceSeconds = 2f / TimelineFramesPerSecond;

    internal readonly record struct ControlIdentity( nint Control, nint Binding, nint Animation, float Duration ) {
        internal bool IsValid => Control != nint.Zero && Binding != nint.Zero && Animation != nint.Zero &&
                                 float.IsFinite( Duration ) && Duration > 0f;
    }

    internal static bool IsSameControl( ControlIdentity left, ControlIdentity right ) =>
        left.IsValid && right.IsValid && left.Control == right.Control && left.Binding == right.Binding &&
        left.Animation == right.Animation && MathF.Abs( left.Duration - right.Duration ) <= 0.0001f;

    internal static int NextReadyObservation( ushort requestedTimeline, ushort actualTimeline,
                                              ControlIdentity previous, ControlIdentity current,
                                              int previousObservations, bool timelineClockSynchronized = true ) {
        if( requestedTimeline != actualTimeline || !current.IsValid || !timelineClockSynchronized ) return 0;
        if( !IsSameControl( previous, current ) ) return 1;
        return Math.Min( previousObservations + 1, RequiredReadyObservations );
    }

    internal static bool IsTimelineClockSynchronized( float controlLocalTime, float schedulerTimestamp,
                                                       float controllerTimestamp ) {
        if( !float.IsFinite( controlLocalTime ) || !float.IsFinite( schedulerTimestamp ) ||
            !float.IsFinite( controllerTimestamp ) ) return false;
        if( MathF.Abs( schedulerTimestamp - controllerTimestamp ) > 0.5f ) return false;
        return MathF.Abs( controlLocalTime - schedulerTimestamp / TimelineFramesPerSecond ) <=
               TimelineClockToleranceSeconds;
    }

    internal static ushort ResetTimelineTarget( ushort originalSlot0Timeline ) =>
        originalSlot0Timeline == 0 ? (ushort)3 : originalSlot0Timeline;

    internal static float TimeTolerance( float fps ) => MathF.Max( 1f / 240f, 1f / fps / 4f );

    internal static bool IsFreezeReadbackValid( ControlIdentity expectedIdentity, ControlIdentity actualIdentity,
                                                 float expectedTime, float actualTime, float fps,
                                                 float overallSpeed, float playbackSpeed ) =>
        IsSameControl( expectedIdentity, actualIdentity ) && float.IsFinite( expectedTime ) &&
        float.IsFinite( actualTime ) && float.IsFinite( fps ) && fps > 0f &&
        MathF.Abs( overallSpeed ) <= 0.0001f && MathF.Abs( playbackSpeed ) <= 0.0001f &&
        MathF.Abs( actualTime - expectedTime ) <= TimeTolerance( fps );

    internal static bool IsFrameReadbackValid( int expectedFrame, float actualTime, float fps ) =>
        expectedFrame >= 0 && float.IsFinite( actualTime ) && float.IsFinite( fps ) && fps > 0f &&
        (int)MathF.Round( actualTime * fps ) == expectedFrame;
}
