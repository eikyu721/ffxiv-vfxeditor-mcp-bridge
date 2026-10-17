using Dalamud.Game.ClientState.Objects.Types;
using Dalamud.Plugin.Ipc;
using FFXIVClientStructs.FFXIV.Client.Game.Character;
using FFXIVClientStructs.FFXIV.Client.Graphics.Scene;
using Lumina.Excel.Sheets;
using System;

namespace VfxEditor;

/// <summary>
/// Owns one actor ActionTimeline override. Native animation inspection is deliberately limited
/// to the main body (partial skeleton 0, animation control 0); face/weapon controls are untouched.
/// All methods and Update must run on Dalamud's framework thread.
/// </summary>
public sealed unsafe class ActorAnimationController : IDisposable {
    private readonly ICallGateSubscriber<(int, int)> BrioVersion;
    private readonly ICallGateSubscriber<IGameObject, float, bool> BrioSetSpeed;
    private readonly ICallGateSubscriber<IGameObject, float> BrioGetSpeed;
    private readonly ICallGateSubscriber<IGameObject, bool> BrioActorExists;
    private readonly ICallGateSubscriber<IGameObject, float, bool> BrioV3SetSpeed;
    private readonly ICallGateSubscriber<IGameObject, float> BrioV3GetSpeed;
    private readonly ICallGateSubscriber<IGameObject, bool> BrioV3ActorExists;

    private IGameObject Actor;
    private string ActorName;
    private nint ActorAddress;
    private ulong ActorId;
    private CharacterModes OriginalMode;
    private byte OriginalModeParam;
    private ushort OriginalBaseOverride;
    private ushort OriginalSlot0TimelineId;
    private ushort TimelineId;
    private string TimelinePath;
    private bool Active;
    private bool Frozen;
    private bool ForceReplay;
    private bool PauseViaBrio;
    private int? RequestedFrame;
    private float? RequestedTime;
    private float RequestedFps = 30f;
    private string LastFailure;
    private long LastForcedReplayAt;
    private long LastMainControlSeenAt;
    private bool HadMainControl;
    private bool LateSeekApplied;
    private long FrameworkUpdateCount;
    private long LateSeekRequestedAtFrameworkUpdate;
    private AnimationFreezePolicy.ControlIdentity ReadyControlCandidate;
    private AnimationFreezePolicy.ControlIdentity PrePlayControlIdentity;
    private bool RequireInitialControlReplacement;
    private int ReadyControlObservations;
    private long ReadyControlLastObservedAt = -1;
    private AnimationFreezePolicy.ControlIdentity FrozenControlIdentity;
    private int SeekWriteCount;

    public readonly record struct FreezeCapability( bool IsGpose, bool BrioApiAvailable, string BrioApiVersion,
        bool BrioManagesActor, float? BrioSpeed, bool CanFreeze, string Backend, string UnavailableReason );

    public ActorAnimationController() {
        BrioVersion = Dalamud.PluginInterface.GetIpcSubscriber<(int, int)>( "Brio.ApiVersion" );
        BrioSetSpeed = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, float, bool>( "Brio.Actor.SetSpeed" );
        BrioGetSpeed = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, float>( "Brio.Actor.GetSpeed" );
        BrioActorExists = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, bool>( "Brio.Actor.Exists" );
        BrioV3SetSpeed = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, float, bool>( "Brio.SetActorSpeed.V3" );
        BrioV3GetSpeed = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, float>( "Brio.GetActorSpeed.V3" );
        BrioV3ActorExists = Dalamud.PluginInterface.GetIpcSubscriber<IGameObject, bool>( "Brio.ActorExists.V3" );
        Dalamud.Framework.Update += OnFrameworkUpdate;
        Dalamud.ClientState.Logout += OnLogout;
    }

    public object Play( ushort timelineId, string timelinePath, bool forceReplay ) {
        if( Active ) throw new InvalidOperationException( "The bridge already owns an actor animation; call reset_actor_animation first." );
        // Entering AnimLock outside GPose can leave the normal-world character in a state that
        // prevents GPose entry. Refuse before resolving the actor or performing any native write.
        if( !Plugin.InGpose )
            throw new InvalidOperationException( "Actor animation playback is restricted to GPose; no character state was changed." );
        var actor = CurrentActor();
        var character = (Character*)actor.Address;
        var row = Dalamud.DataManager.GetExcelSheet<ActionTimeline>().GetRowOrDefault( timelineId );
        if( row == null || string.IsNullOrWhiteSpace( row.Value.Key.ToString() ) )
            throw new InvalidOperationException( $"ActionTimeline {timelineId} does not exist or has no playable key." );

        var freezeCapability = ProbeFreezeCapability( actor );
        if( freezeCapability.BrioManagesActor && !freezeCapability.CanFreeze )
            throw new InvalidOperationException( freezeCapability.UnavailableReason );

        Actor = actor;
        ActorName = actor.Name.ToString();
        ActorAddress = actor.Address;
        ActorId = Convert.ToUInt64( actor.GameObjectId );
        OriginalMode = character->Mode;
        OriginalModeParam = character->ModeParam;
        OriginalBaseOverride = character->Timeline.BaseOverride;
        OriginalSlot0TimelineId = character->Timeline.TimelineSequencer.TimelineIds[0];
        TimelineId = timelineId;
        TimelinePath = timelinePath;
        ForceReplay = forceReplay;
        Frozen = false;
        RequestedFrame = null;
        RequestedTime = null;
        LastFailure = null;
        HadMainControl = false;
        LastForcedReplayAt = 0;
        LastMainControlSeenAt = 0;
        ReadyControlCandidate = default;
        ReadyControlObservations = 0;
        ReadyControlLastObservedAt = -1;
        FrozenControlIdentity = default;
        SeekWriteCount = 0;
        TryControlIdentity( out PrePlayControlIdentity );
        RequireInitialControlReplacement = OriginalSlot0TimelineId != timelineId && PrePlayControlIdentity.IsValid;
        Active = true;

        character->SetMode( CharacterModes.AnimLock, 0 );
        character->Timeline.BaseOverride = timelineId;
        character->Timeline.TimelineSequencer.PlayTimeline( timelineId );
        character->Timeline.OverallSpeed = 1f;
        return State( "play-issued" );
    }

    public object BeginFreeze() {
        RequireOwnedActor();
        var identity = RequireReadyControl();
        var capability = ProbeFreezeCapability( Actor );
        if( !capability.CanFreeze ) throw new InvalidOperationException( capability.UnavailableReason );
        if( !InvokeBrioSetSpeed( Actor, 0f ) ) throw new InvalidOperationException( "Brio rejected the pause request." );
        PauseViaBrio = true;
        Frozen = true;
        FrozenControlIdentity = identity;
        SeekWriteCount = 0;
        return State( "pause-issued" );
    }

    public object ValidateFrame( int frame, float fps ) {
        RequireOwnedActor();
        var identity = RequireReadyControl();
        var duration = identity.Duration;
        var time = frame / fps;
        if( time > duration + 0.0001f )
            throw new InvalidOperationException( $"frame {frame} at {fps} fps is {time:F4}s, beyond the main control duration {duration:F4}s." );
        return new { ok = true, durationSeconds = duration, requestedTimeSeconds = time };
    }

    public object MainControlIsReady() {
        RequireOwnedActor();
        return ObserveMainControlReadiness();
    }

    private bool ObserveMainControlReadiness() {
        if( ReadyControlLastObservedAt == FrameworkUpdateCount ) return IsCurrentControlReady();
        ReadyControlLastObservedAt = FrameworkUpdateCount;
        var character = NativeActor();
        if( !TryTimelineControlState( out var identity, out _, out _, out _ ) ) {
            ReadyControlCandidate = default;
            ReadyControlObservations = 0;
            return false;
        }
        if( RequireInitialControlReplacement ) {
            if( AnimationFreezePolicy.IsSameControl( PrePlayControlIdentity, identity ) ) {
                ReadyControlCandidate = default;
                ReadyControlObservations = 0;
                return false;
            }
            RequireInitialControlReplacement = false;
            ReadyControlCandidate = default;
            ReadyControlObservations = 0;
        }
        var clockSynchronized = IsTimelineClockSynchronized();
        ReadyControlObservations = AnimationFreezePolicy.NextReadyObservation(
            TimelineId, character->Timeline.TimelineSequencer.TimelineIds[0],
            ReadyControlCandidate, identity, ReadyControlObservations, clockSynchronized );
        ReadyControlCandidate = ReadyControlObservations == 0 ? default : identity;
        return ReadyControlObservations >= AnimationFreezePolicy.RequiredReadyObservations;
    }

    private bool IsCurrentControlReady() => !RequireInitialControlReplacement &&
        NativeActor()->Timeline.TimelineSequencer.TimelineIds[0] == TimelineId &&
        ReadyControlObservations >= AnimationFreezePolicy.RequiredReadyObservations &&
        TryControlIdentity( out var identity ) && AnimationFreezePolicy.IsSameControl( ReadyControlCandidate, identity ) &&
        IsTimelineClockSynchronized();

    public object EnsureTimelineEvaluatingForFreeze() {
        RequireOwnedActor();
        if( IsTimelineClockSynchronized() ) return new { ok = true, replayIssued = false };
        // A non-looping timeline can retain its endpoint Havok control after its scheduler has
        // advanced or restarted. Reissue only when the two clocks disagree, then require fresh
        // synchronized observations before pausing or seeking.
        NativeActor()->Timeline.TimelineSequencer.PlayTimeline( TimelineId );
        ReadyControlCandidate = default;
        ReadyControlObservations = 0;
        ReadyControlLastObservedAt = -1;
        return new { ok = true, replayIssued = true };
    }

    public FreezeCapability GetFreezeCapability() => ProbeFreezeCapability( TryCurrentActor() );

    public object RequireFreezeAvailable() {
        RequireOwnedActor();
        var capability = ProbeFreezeCapability( Actor );
        if( !capability.CanFreeze ) throw new InvalidOperationException( capability.UnavailableReason );
        return new { ok = true, isGpose = capability.IsGpose, canFreeze = true, freezeBackend = capability.Backend };
    }

    public object SeekFrame( int frame, float fps ) {
        RequireOwnedActor();
        if( !Frozen ) throw new InvalidOperationException( "The bridge does not own a freeze. Call freeze_actor_animation." );
        var control = MainControl( true );
        RequireFrozenControl( control );
        var time = frame / fps;
        var duration = control->hkaAnimationControl.Binding.ptr->Animation.ptr->Duration;
        time = Math.Clamp( time, 0f, duration );
        RequestedFrame = frame;
        RequestedFps = fps;
        RequestedTime = time;
        LateSeekApplied = false;
        LateSeekRequestedAtFrameworkUpdate = FrameworkUpdateCount;
        // Match Brio's reviewed scrub path: once playback is paused, only the Havok control's
        // local time is changed. Scheduler timestamps belong to the TMB timeline and are kept
        // diagnostic-only; forcing them can cause a later native evaluation to replace this seek.
        control->hkaAnimationControl.LocalTime = time;
        LateSeekApplied = true;
        SeekWriteCount++;
        return State( "havok-local-time-only-seek-issued" );
    }

    public object MainControlIsPaused() {
        RequireOwnedActor();
        if( !Frozen ) return false;
        var control = MainControl( true );
        RequireFrozenControl( control );
        return MathF.Abs( NativeActor()->Timeline.OverallSpeed ) < 0.0001f && MathF.Abs( control->PlaybackSpeed ) < 0.0001f;
    }

    public object FreezeReadbackMatches() {
        RequireOwnedActor();
        if( !Frozen || !RequestedTime.HasValue || !LateSeekApplied ) return false;
        var control = MainControl( true );
        var actualIdentity = ControlIdentity( control );
        return AnimationFreezePolicy.IsFreezeReadbackValid(
            FrozenControlIdentity, actualIdentity, RequestedTime.Value, control->hkaAnimationControl.LocalTime,
            RequestedFps, NativeActor()->Timeline.OverallSpeed, control->PlaybackSpeed ) &&
            AnimationFreezePolicy.IsFrameReadbackValid( RequestedFrame.GetValueOrDefault( -1 ),
                control->hkaAnimationControl.LocalTime, RequestedFps );
    }

    public object VerifyFreeze() {
        RequireOwnedActor();
        var control = MainControl( true );
        var actualIdentity = ControlIdentity( control );
        var actual = control->hkaAnimationControl.LocalTime;
        var playbackSpeed = control->PlaybackSpeed;
        var overallSpeed = NativeActor()->Timeline.OverallSpeed;
        var scheduler = NativeActor()->Timeline.TimelineSequencer.GetSchedulerTimeline( 0 );
        var schedulerTime = scheduler == null ? float.NaN : scheduler->CurrentTimestamp;
        var controllerTime = scheduler == null ? float.NaN : scheduler->TimelineController.CurrentTimestamp;
        var actualFrame = (int)MathF.Round( actual * RequestedFps );
        if( !Frozen || !RequestedTime.HasValue || !LateSeekApplied ||
            !AnimationFreezePolicy.IsFreezeReadbackValid( FrozenControlIdentity, actualIdentity,
                RequestedTime.GetValueOrDefault(), actual, RequestedFps, overallSpeed, playbackSpeed ) ||
            !AnimationFreezePolicy.IsFrameReadbackValid( RequestedFrame.GetValueOrDefault( -1 ), actual, RequestedFps ) )
            throw new InvalidOperationException( $"Freeze readback failed after {SeekWriteCount} bounded local-time write(s): requested frame/time {RequestedFrame}/{RequestedTime:F4}s, actual frame/time {actualFrame}/{actual:F4}s, overallSpeed={overallSpeed:F4}, controlPlaybackSpeed={playbackSpeed:F4}, controlIdentityMatch={AnimationFreezePolicy.IsSameControl( FrozenControlIdentity, actualIdentity )}, schedulerDiagnostic={schedulerTime:F3}/{controllerTime:F3}." );
        return State( "frozen-and-verified" );
    }

    public void AbortFreeze() {
        if( !Active || !Frozen ) return;
        if( !IsActorStable() ) {
            LastFailure = "Owned actor disappeared before freeze cleanup; bridge ownership was discarded without dereferencing stale game memory.";
            Clear();
            return;
        }
        try { ReleasePause(); }
        catch( Exception e ) {
            LastFailure = $"Freeze cleanup could not clear Brio speed normally: {e.GetBaseException().Message}";
            try { NativeActor()->Timeline.OverallSpeed = 1f; }
            catch { }
        }
        finally { ClearFreezeState(); }
    }

    public float BeginResume() {
        RequireOwnedActor();
        if( !Frozen ) throw new InvalidOperationException( "The bridge does not currently own a freeze." );
        var control = MainControl( true );
        var before = control->hkaAnimationControl.LocalTime;
        var duration = control->hkaAnimationControl.Binding.ptr->Animation.ptr->Duration;
        if( before >= duration - 1f / 60f ) {
            NativeActor()->Timeline.TimelineSequencer.PlayTimeline( TimelineId );
            before = 0f;
        }
        ReleasePause();
        Frozen = false;
        RequestedFrame = null;
        RequestedTime = null;
        LateSeekApplied = false;
        FrozenControlIdentity = default;
        SeekWriteCount = 0;
        return before;
    }

    public object VerifyResume( float before ) {
        RequireOwnedActor();
        var control = MainControl( true );
        var actual = control->hkaAnimationControl.LocalTime;
        if( MathF.Abs( actual - before ) < 0.0005f )
            throw new InvalidOperationException( $"Resume readback failed: main control remained at {actual:F4}s." );
        return State( "resumed-and-verified" );
    }

    public void ReleasePauseBeforeReset() {
        RequireOwnedActor();
        ReleasePause();
        ClearFreezeState();
    }

    public object ApplyReset() {
        RequireOwnedActor();
        ForceReplay = false;
        var character = NativeActor();
        character->Timeline.BaseOverride = OriginalBaseOverride;
        // Play entered AnimLock through SetMode; leave it through the same game function so
        // mode-transition side effects are cleared too. Direct field restoration can read back
        // as Normal while the client still rejects GPose as an occupied/mechanical state.
        character->SetMode( OriginalMode, OriginalModeParam );
        character->Timeline.TimelineSequencer.PlayTimeline( AnimationFreezePolicy.ResetTimelineTarget( OriginalSlot0TimelineId ) );
        return new { ok = true, phase = "reset-issued" };
    }

    public object VerifyReset() {
        RequireOwnedActor();
        var character = NativeActor();
        var slot0 = character->Timeline.TimelineSequencer.TimelineIds[0];
        var speed = character->Timeline.OverallSpeed;
        var modeRestored = character->Mode == OriginalMode && character->ModeParam == OriginalModeParam;
        var baseRestored = character->Timeline.BaseOverride == OriginalBaseOverride;
        var resetTimelineTarget = AnimationFreezePolicy.ResetTimelineTarget( OriginalSlot0TimelineId );
        var timelineReleased = slot0 == resetTimelineTarget;
        var pauseReleased = speed > 0.0001f;
        if( !modeRestored || !baseRestored || !timelineReleased || !pauseReleased ) {
            var error = $"Reset readback failed: mode={character->Mode}/{character->ModeParam}, base={character->Timeline.BaseOverride}, slot0={slot0} (expected pre-takeover target {resetTimelineTarget}), speed={speed:F3}.";
            Clear();
            throw new InvalidOperationException( error );
        }
        var result = new {
            ok = true, phase = "reset-and-verified", actor = ActorInfo(), releasedPause = pauseReleased,
            restoredMode = OriginalMode.ToString(), restoredModeParam = OriginalModeParam,
            restoredBaseOverride = OriginalBaseOverride, originalSlot0TimelineId = OriginalSlot0TimelineId,
            resetTimelineTarget, actualSlot0TimelineId = slot0, actualOverallSpeed = speed,
            modeReadbackVerified = modeRestored, baseReadbackVerified = baseRestored, timelineReleasedVerified = timelineReleased,
            brioSpeedOverrideMayRemainAtOne = PauseViaBrio,
            limitation = Limitation
        };
        Clear();
        return result;
    }

    public object State( string phase = "query" ) {
        if( !Active ) {
            var current = TryCurrentActor();
            var inactiveCapability = ProbeFreezeCapability( current );
            return new { ok = true, phase, active = false, bridgeOwnsFreeze = false,
                canPlay = inactiveCapability.IsGpose,
                playUnavailableReason = inactiveCapability.IsGpose ? null : "Actor animation playback is restricted to GPose.",
                isGpose = inactiveCapability.IsGpose, canFreeze = inactiveCapability.CanFreeze, freezeBackend = inactiveCapability.Backend,
                freezeUnavailableReason = inactiveCapability.UnavailableReason, brioApiAvailable = inactiveCapability.BrioApiAvailable,
                brioApiVersion = inactiveCapability.BrioApiVersion,
                brioManagesActor = inactiveCapability.BrioManagesActor, brioSpeed = inactiveCapability.BrioSpeed,
                limitation = Limitation, lastFailure = LastFailure };
        }
        var actorStable = IsActorStable();
        if( !actorStable ) return new { ok = true, phase, active = true, actor = ActorInfo(), actorStable = false,
            timelineId = TimelineId, timelinePath = TimelinePath, bridgeOwnsFreeze = Frozen,
            isGpose = Plugin.InGpose, canFreeze = false, freezeBackend = "unavailable",
            freezeUnavailableReason = "Owned actor identity changed or became invalid.",
            error = "Owned actor identity changed or became invalid.", limitation = Limitation };

        var character = NativeActor();
        var capability = ProbeFreezeCapability( Actor );
        var control = MainControl( false );
        var scheduler = character->Timeline.TimelineSequencer.GetSchedulerTimeline( 0 );
        var frozenControlMatches = Frozen && control != null &&
            AnimationFreezePolicy.IsSameControl( FrozenControlIdentity, ControlIdentity( control ) );
        // Once a verified seek is frozen, LocalTime intentionally differs from the still-diagnostic
        // scheduler clock. At that point identity ownership, not pre-freeze clock agreement, tells us
        // whether this is still the requested control.
        var requestedControlReady = Frozen ? frozenControlMatches : IsCurrentControlReady();
        float? localTime = control == null ? null : control->hkaAnimationControl.LocalTime;
        float? duration = control == null || control->hkaAnimationControl.Binding.ptr == null || control->hkaAnimationControl.Binding.ptr->Animation.ptr == null
            ? null : control->hkaAnimationControl.Binding.ptr->Animation.ptr->Duration;
        int? actualFrame = localTime.HasValue ? (int)MathF.Round( localTime.Value * RequestedFps ) : null;
        return new { ok = true, phase, active = true, actor = ActorInfo(), actorStable = true,
            timelineId = TimelineId, timelinePath = TimelinePath, currentBaseOverride = character->Timeline.BaseOverride,
            currentSlot0TimelineId = character->Timeline.TimelineSequencer.TimelineIds[0],
            currentSlot0Speed = character->Timeline.TimelineSequencer.TimelineSpeeds[0],
            mainControlReady = control != null, partialIndex = 0, controlIndex = 0, localTimeSeconds = localTime,
            durationSeconds = duration, overallSpeed = character->Timeline.OverallSpeed,
            requestedTimelineControlReady = requestedControlReady,
            reportedControlMayBelongToPreviousTimeline = !Frozen && control != null && !requestedControlReady,
            timelineClockSynchronized = control != null && scheduler != null &&
                AnimationFreezePolicy.IsTimelineClockSynchronized( control->hkaAnimationControl.LocalTime,
                    scheduler->CurrentTimestamp, scheduler->TimelineController.CurrentTimestamp ),
            originalSlot0TimelineId = OriginalSlot0TimelineId,
            requiresInitialControlReplacement = RequireInitialControlReplacement,
            mainControlPlaybackSpeed = control == null ? (float?)null : control->PlaybackSpeed,
            schedulerReady = scheduler != null,
            schedulerCurrentTimestamp = scheduler == null ? (float?)null : scheduler->CurrentTimestamp,
            schedulerControllerCurrentTimestamp = scheduler == null ? (float?)null : scheduler->TimelineController.CurrentTimestamp,
            isGpose = capability.IsGpose, canFreeze = Frozen || capability.CanFreeze,
            freezeBackend = Frozen ? "brio-ipc" : capability.Backend,
            freezeUnavailableReason = Frozen ? null : capability.UnavailableReason,
            brioApiAvailable = capability.BrioApiAvailable, brioApiVersion = capability.BrioApiVersion,
            brioManagesActor = capability.BrioManagesActor,
            brioSpeed = capability.BrioSpeed,
            bridgeOwnsFreeze = Frozen, pauseBackend = Frozen ? "brio-ipc" : null,
            requestedFrame = RequestedFrame, requestedFps = RequestedFrame.HasValue ? RequestedFps : (float?)null,
            requestedTimeSeconds = RequestedTime, actualFrame,
            actualFrameFps = RequestedFps,
            seekWritePhase = "framework-havok-local-time-only-bounded", seekWriteCount = SeekWriteCount,
            maxSeekAttempts = AnimationFreezePolicy.MaxSeekAttempts,
            readyControlObservations = ReadyControlObservations,
            requiredReadyObservations = AnimationFreezePolicy.RequiredReadyObservations,
            lateSeekApplied = LateSeekApplied, lateSeekPending = false,
            frameworkUpdateCount = FrameworkUpdateCount,
            frameworkUpdatesSinceSeek = RequestedTime.HasValue ? FrameworkUpdateCount - LateSeekRequestedAtFrameworkUpdate : (long?)null,
            loopMode = ForceReplay ? "forced-replay" : "native-or-none", nativeLoopKnown = false,
            limitation = Limitation };
    }

    private const string Limitation = "Only main body partial 0 / control 0 is inspected and scrubbed; face, weapon and other composite controls are not synchronized.";

    private static IGameObject CurrentActor() {
        var actor = TryCurrentActor();
        if( actor == null || actor.Address == nint.Zero ) throw new InvalidOperationException( "No valid local-player/GPose target actor is available." );
        return actor;
    }

    private static IGameObject TryCurrentActor() {
        try { return Plugin.PlayerObject; }
        catch { return null; }
    }

    private Character* NativeActor() => (Character*)ActorAddress;

    private void RequireOwnedActor() {
        if( !Active ) throw new InvalidOperationException( "The bridge does not own an actor animation. Call play_actor_animation first." );
        if( !IsActorStable() ) {
            LastFailure = "Owned actor identity changed, disappeared, or logged out; bridge ownership was discarded.";
            Clear();
            throw new InvalidOperationException( LastFailure );
        }
    }

    private bool IsActorStable() {
        try {
            if( !Dalamud.ClientState.IsLoggedIn || ActorAddress == nint.Zero ) return false;
            // Resolve the current object again instead of dereferencing the retained Dalamud
            // wrapper, which can throw after logout or an object-table rebuild.
            var current = TryCurrentActor();
            return current != null && current.Address == ActorAddress && Convert.ToUInt64( current.GameObjectId ) == ActorId;
        }
        catch { return false; }
    }

    private object ActorInfo() => new { name = ActorName, gameObjectId = $"0x{ActorId:X}", address = $"0x{ActorAddress:X}" };

    private FFXIVClientStructs.Havok.Animation.Playback.Control.Default.hkaDefaultAnimationControl* MainControl( bool required ) {
        var drawObject = NativeActor()->GameObject.DrawObject;
        if( drawObject == null || drawObject->Object.GetObjectType() != ObjectType.CharacterBase ) return MissingControl( required );
        var characterBase = (CharacterBase*)drawObject;
        if( characterBase->Skeleton == null || characterBase->Skeleton->PartialSkeletonCount < 1 ) return MissingControl( required );
        var animated = characterBase->Skeleton->PartialSkeletons[0].GetHavokAnimatedSkeleton( 0 );
        if( animated == null || animated->AnimationControls.Length < 1 ) return MissingControl( required );
        var control = animated->AnimationControls[0].Value;
        if( control == null || control->hkaAnimationControl.Binding.ptr == null || control->hkaAnimationControl.Binding.ptr->Animation.ptr == null ) return MissingControl( required );
        return control;
    }

    private bool TryControlIdentity( out AnimationFreezePolicy.ControlIdentity identity ) {
        var control = MainControl( false );
        if( control == null ) {
            identity = default;
            return false;
        }
        identity = ControlIdentity( control );
        return identity.IsValid;
    }

    private bool TryTimelineControlState( out AnimationFreezePolicy.ControlIdentity identity, out float localTime,
                                          out float schedulerTimestamp, out float controllerTimestamp ) {
        var control = MainControl( false );
        var scheduler = NativeActor()->Timeline.TimelineSequencer.GetSchedulerTimeline( 0 );
        if( control == null || scheduler == null ) {
            identity = default;
            localTime = schedulerTimestamp = controllerTimestamp = float.NaN;
            return false;
        }
        identity = ControlIdentity( control );
        localTime = control->hkaAnimationControl.LocalTime;
        schedulerTimestamp = scheduler->CurrentTimestamp;
        controllerTimestamp = scheduler->TimelineController.CurrentTimestamp;
        return identity.IsValid;
    }

    private bool IsTimelineClockSynchronized() =>
        TryTimelineControlState( out _, out var localTime, out var schedulerTimestamp, out var controllerTimestamp ) &&
        AnimationFreezePolicy.IsTimelineClockSynchronized( localTime, schedulerTimestamp, controllerTimestamp );

    private static AnimationFreezePolicy.ControlIdentity ControlIdentity(
        FFXIVClientStructs.Havok.Animation.Playback.Control.Default.hkaDefaultAnimationControl* control ) {
        if( control == null || control->hkaAnimationControl.Binding.ptr == null ||
            control->hkaAnimationControl.Binding.ptr->Animation.ptr == null ) return default;
        return new( (nint)control, (nint)control->hkaAnimationControl.Binding.ptr,
            (nint)control->hkaAnimationControl.Binding.ptr->Animation.ptr,
            control->hkaAnimationControl.Binding.ptr->Animation.ptr->Duration );
    }

    private AnimationFreezePolicy.ControlIdentity RequireReadyControl() {
        if( NativeActor()->Timeline.TimelineSequencer.TimelineIds[0] != TimelineId ||
            ReadyControlObservations < AnimationFreezePolicy.RequiredReadyObservations ||
            !TryControlIdentity( out var identity ) ||
            !AnimationFreezePolicy.IsSameControl( ReadyControlCandidate, identity ) )
            throw new InvalidOperationException( "Requested slot-0 timeline and main control are not stable for two framework observations." );
        return identity;
    }

    private void RequireFrozenControl(
        FFXIVClientStructs.Havok.Animation.Playback.Control.Default.hkaDefaultAnimationControl* control ) {
        var current = ControlIdentity( control );
        if( !AnimationFreezePolicy.IsSameControl( FrozenControlIdentity, current ) )
            throw new InvalidOperationException( "Main animation control identity changed during freeze; refusing to write a replacement control." );
    }

    private static FFXIVClientStructs.Havok.Animation.Playback.Control.Default.hkaDefaultAnimationControl* MissingControl( bool required ) {
        if( required ) throw new InvalidOperationException( "Main body animation control (partial 0 / control 0) is not ready." );
        return null;
    }

    private FreezeCapability ProbeFreezeCapability( IGameObject actor ) {
        var isGpose = Plugin.InGpose;
        try {
            var version = BrioVersion.InvokeFunc();
            var versionText = $"{version.Item1}.{version.Item2}";
            // Brio 3.0 source still registers Actor.Exists/GetSpeed/SetSpeed with the same
            // IGameObject -> bool/float and IGameObject,float -> bool contracts as API 2.
            if( version.Item1 is not ( 2 or 3 ) ) return new( isGpose, true, versionText, false, null, false, "unavailable",
                $"Brio IPC major version {version.Item1} is unsupported; reviewed contracts are API 2 and 3." );
            if( !isGpose ) return new( false, true, versionText, false, null, false, "unavailable",
                "Frame freeze requires GPose and a Brio-managed actor; normal-mode native freezing is not supported." );
            if( actor == null || actor.Address == nint.Zero ) return new( true, true, versionText, false, null, false, "unavailable",
                "No valid GPose target actor is available." );
            if( !InvokeBrioActorExists( version.Item1, actor ) ) return new( true, true, versionText, false, null, false, "unavailable",
                "Brio is online but does not manage the current GPose target actor." );
            var speed = InvokeBrioGetSpeed( version.Item1, actor );
            if( MathF.Abs( speed - 1f ) > 0.0001f ) return new( true, true, versionText, true, speed, false, "brio-ipc",
                $"Brio already owns a persistent speed override of {speed:F3}; reset it to 1 before freezing." );
            return new( true, true, versionText, true, speed, true, "brio-ipc", null );
        }
        catch( Exception e ) { return new( isGpose, false, null, false, null, false, "unavailable",
            $"Brio animation IPC is unavailable: {e.GetBaseException().Message}" ); }
    }

    private void ReleasePause() {
        if( PauseViaBrio ) {
            if( !InvokeBrioSetSpeed( Actor, 1f ) ) throw new InvalidOperationException( "Brio rejected the resume request." );
        }
    }

    private bool InvokeBrioActorExists( int major, IGameObject actor ) => major == 3
        ? BrioV3ActorExists.InvokeFunc( actor )
        : BrioActorExists.InvokeFunc( actor );

    private float InvokeBrioGetSpeed( int major, IGameObject actor ) => major == 3
        ? BrioV3GetSpeed.InvokeFunc( actor )
        : BrioGetSpeed.InvokeFunc( actor );

    private bool InvokeBrioSetSpeed( IGameObject actor, float speed ) {
        var version = BrioVersion.InvokeFunc();
        return version.Item1 == 3 ? BrioV3SetSpeed.InvokeFunc( actor, speed ) : BrioSetSpeed.InvokeFunc( actor, speed );
    }

    private void OnFrameworkUpdate( global::Dalamud.Plugin.Services.IFramework framework ) {
        FrameworkUpdateCount++;
        if( !Active ) return;
        try {
            if( !IsActorStable() ) {
                LastFailure = "Owned actor disappeared or the client logged out; bridge ownership was discarded without touching stale game memory.";
                Clear();
                return;
            }
            ObserveMainControlReadiness();
            var control = MainControl( false );
            if( Frozen && ( control == null ||
                !AnimationFreezePolicy.IsSameControl( FrozenControlIdentity, ControlIdentity( control ) ) ) ) {
                LastFailure = "Main animation control identity changed while frozen; bridge pause ownership was released.";
                AbortFreeze();
                return;
            }
            if( !Frozen && ForceReplay ) {
                var now = Environment.TickCount64;
                if( control != null ) {
                    HadMainControl = true;
                    LastMainControlSeenAt = now;
                    var duration = control->hkaAnimationControl.Binding.ptr->Animation.ptr->Duration;
                    if( duration > 0 && control->hkaAnimationControl.LocalTime >= duration - 1f / 60f && now - LastForcedReplayAt > 250 ) {
                        LastForcedReplayAt = now;
                        HadMainControl = false;
                        NativeActor()->Timeline.TimelineSequencer.PlayTimeline( TimelineId );
                    }
                }
                else if( HadMainControl && now - LastMainControlSeenAt > 100 && now - LastForcedReplayAt > 250 ) {
                    // Some non-looping timelines remove their control at the end instead of exposing duration.
                    LastForcedReplayAt = now;
                    HadMainControl = false;
                    NativeActor()->Timeline.TimelineSequencer.PlayTimeline( TimelineId );
                }
            }
        }
        catch( Exception e ) { LastFailure = e.GetBaseException().Message; }
    }

    private void OnLogout( int type, int code ) {
        if( !Active ) return;
        // The actor address is no longer safe to touch once logout begins. Do not attempt a
        // native reset or Brio IPC cleanup here; simply forget all ownership synchronously.
        LastFailure = $"Client logout ({type}, {code}) discarded actor animation ownership.";
        Clear();
    }

    private void Clear() {
        Active = Frozen = ForceReplay = PauseViaBrio = false;
        Actor = null;
        ActorName = null;
        ActorAddress = nint.Zero;
        ActorId = 0;
        OriginalSlot0TimelineId = 0;
        TimelineId = 0;
        TimelinePath = null;
        RequestedFrame = null;
        RequestedTime = null;
        LateSeekApplied = false;
        ReadyControlCandidate = default;
        PrePlayControlIdentity = default;
        RequireInitialControlReplacement = false;
        ReadyControlObservations = 0;
        ReadyControlLastObservedAt = -1;
        FrozenControlIdentity = default;
        SeekWriteCount = 0;
    }

    private void ClearFreezeState() {
        Frozen = false;
        RequestedFrame = null;
        RequestedTime = null;
        LateSeekApplied = false;
        FrozenControlIdentity = default;
        SeekWriteCount = 0;
    }

    public void Dispose() {
        Dalamud.Framework.Update -= OnFrameworkUpdate;
        Dalamud.ClientState.Logout -= OnLogout;
        if( Active && IsActorStable() ) {
            try {
                ReleasePause();
            }
            catch { NativeActor()->Timeline.OverallSpeed = 1f; }
            try {
                var character = NativeActor();
                character->Timeline.BaseOverride = OriginalBaseOverride;
                character->SetMode( OriginalMode, OriginalModeParam );
                character->Timeline.TimelineSequencer.PlayTimeline( AnimationFreezePolicy.ResetTimelineTarget( OriginalSlot0TimelineId ) );
            }
            catch { }
        }
        Clear();
    }
}
