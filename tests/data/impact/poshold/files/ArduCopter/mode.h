










class Parameters;
class ParametersG2;

class GCS_Copter;



class _AutoTakeoff {
















};


class PayloadPlace {





    enum class State : uint8_t {









    };












};


class Mode {
    friend class PayloadPlace;




    enum class Number : uint8_t {































    };







    friend class _AutoTakeoff;





    virtual bool init(bool ignore_checks) {

    }
    virtual void exit() {};





    virtual bool has_user_takeoff(bool must_navigate) const { return false; }
    virtual bool in_guided_mode() const { return false; }
    virtual bool logs_attitude() const { return false; }
    virtual bool allows_save_trim() const { return false; }
    virtual bool allows_auto_trim() const { return false; }
    virtual bool allows_autotune() const { return false; }
    virtual bool allows_flip() const { return false; }
    virtual bool crash_check_enabled() const { return true; }


    virtual bool allows_entry_in_rc_failsafe() const { return true; }



    virtual AP_AdvancedFailsafe_Copter::control_mode afs_mode() const { return AP_AdvancedFailsafe_Copter::control_mode::AFS_STABILIZED; }



    virtual bool allows_GCS_or_SCR_arming_with_throttle_high() const { return false; }


    virtual bool allows_inverted() const { return false; };








    static void takeoff_stop() { takeoff.stop(); }

    virtual bool is_landing() const { return false; }


    virtual bool requires_terrain_failsafe() const { return false; }


    virtual bool get_wp(Location &loc) const { return false; };
    virtual float wp_bearing_deg() const { return 0; }
    virtual float wp_distance_m() const { return 0.0f; }
    virtual float crosstrack_error_m() const { return 0.0f;}


    virtual bool set_speed_NE_ms(float speed_ne_ms) {return false;}
    virtual bool set_speed_up_ms(float speed_up_ms) {return false;}
    virtual bool set_speed_down_ms(float speed_down_ms) {return false;}

















    virtual bool use_pilot_yaw() const {return true; }


    virtual bool pause() { return false; };
    virtual bool resume() { return false; };






    virtual bool allows_weathervaning() const { return false; }















    void land_run_horiz_and_vert_control(bool pause_descent = false) {


    }
























    enum class AltHoldModeState {





    };



























    class _TakeOff {






        bool running() const { return _running; }




    };









    class AutoYaw {




        enum class Mode {











        };


        Mode mode() const { return _mode; }






























































    };




    // class.










};



class ModeAcro : public Mode {




    Number mode_number() const override { return Number::ACRO; }

    enum class Trainer {



    };

    enum class AcroOptions {


    };



    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return true; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }




    bool allows_save_trim() const override { return true; }
    bool allows_flip() const override { return true; }
    bool crash_check_enabled() const override { return false; }
    bool allows_entry_in_rc_failsafe() const override { return false; }



    const char *name() const override { return "ACRO"; }
    const char *name4() const override { return "ACRO"; }









};



class ModeAcro_Heli : public ModeAcro {











};



class ModeAltHold : public Mode {




    Number mode_number() const override { return Number::ALT_HOLD; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool has_user_takeoff(bool must_navigate) const override {

    }
    bool allows_autotune() const override { return true; }
    bool allows_flip() const override { return true; }
    bool allows_auto_trim() const override { return true; }
    bool allows_save_trim() const override { return true; }

    bool allows_inverted() const override { return true; };



    const char *name() const override { return "ALT_HOLD"; }
    const char *name4() const override { return "ALTH"; }



};

class ModeAuto : public Mode {


    friend class PayloadPlace;  // in case wp_run is accidentally required



    Number mode_number() const override { return auto_RTL? Number::AUTO_RTL : Number::AUTO; }






    bool has_manual_throttle() const override { return false; }

    bool is_autopilot() const override { return true; }
    bool in_guided_mode() const override { return _mode == SubMode::NAVGUIDED || _mode == SubMode::NAV_SCRIPT_TIME; }

    bool allows_inverted() const override { return true; };




    AP_AdvancedFailsafe_Copter::control_mode afs_mode() const override { return AP_AdvancedFailsafe_Copter::control_mode::AFS_AUTO; }



    bool allows_GCS_or_SCR_arming_with_throttle_high() const override { return true; }


    enum class SubMode : uint8_t {














    };



























    bool requires_terrain_failsafe() const override { return true; }



















    AP_Mission mission{


        FUNCTOR_BIND_MEMBER(&ModeAuto::exit_mission, void)};














    const char *name() const override { return auto_RTL? "AUTO RTL" : "AUTO"; }
    const char *name4() const override { return auto_RTL? "ARTL" : "AUTO"; }



    float crosstrack_error_m() const override { return wp_nav->crosstrack_error_m();}




    enum class Option : int32_t {




    };





























































































    struct {





    } loiter_to_alt;










    enum class State {


    };









    struct {









    } nav_scripting;



    struct {





    } nav_attitude_time;


    struct {



    } desired_speed_override_ms;


};



  wrapper class for AC_AutoTune



class AutoTune : public AC_AutoTune_Heli

class AutoTune : public AC_AutoTune_Multi

{












};

class ModeAutoTune : public Mode {


    friend class ParametersG2;




    Number mode_number() const override { return Number::AUTOTUNE; }





    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; }
    bool is_autopilot() const override { return false; }





    const char *name() const override { return "AUTOTUNE"; }
    const char *name4() const override { return "ATUN"; }
};



class ModeBrake : public Mode {




    Number mode_number() const override { return Number::BRAKE; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return true; }





    const char *name() const override { return "BRAKE"; }
    const char *name4() const override { return "BRAK"; }






};


class ModeCircle : public Mode {




    Number mode_number() const override { return Number::CIRCLE; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return true; }



    const char *name() const override { return "CIRCLE"; }
    const char *name4() const override { return "CIRC"; }








};


class ModeDrift : public Mode {




    Number mode_number() const override { return Number::DRIFT; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool allows_entry_in_rc_failsafe() const override { return false; }



    const char *name() const override { return "DRIFT"; }
    const char *name4() const override { return "DRIF"; }





};


class ModeFlip : public Mode {




    Number mode_number() const override { return Number::FLIP; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return false; }
    bool crash_check_enabled() const override { return false; }



    const char *name() const override { return "FLIP"; }
    const char *name4() const override { return "FLIP"; }






    enum class FlipState : uint8_t {






    };





};




  class to support FLOWHOLD mode, which is a position hold mode using



class ModeFlowHold : public Mode {



    Number mode_number() const override { return Number::FLOWHOLD; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool has_user_takeoff(bool must_navigate) const override {

    }
    bool allows_flip() const override { return true; }

    static const struct AP_Param::GroupInfo var_info[];


    const char *name() const override { return "FLOWHOLD"; }
    const char *name4() const override { return "FHLD"; }




    enum FlowHoldModeState {




    };


















    AC_PI_2D flow_pi_xy{0.2f, 0.3f, 3000, 5, 0.0025f};



























};



class ModeGuided : public Mode {



    friend class AP_ExternalControl_Copter;




    Number mode_number() const override { return Number::GUIDED; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }

    bool is_autopilot() const override { return true; }
    bool has_user_takeoff(bool must_navigate) const override { return true; }
    bool in_guided_mode() const override { return true; }

    bool requires_terrain_failsafe() const override { return true; }



    AP_AdvancedFailsafe_Copter::control_mode afs_mode() const override { return AP_AdvancedFailsafe_Copter::control_mode::AFS_AUTO; }



    bool allows_GCS_or_SCR_arming_with_throttle_high() const override { return true; }













































    enum class SubMode {







    };

    SubMode submode() const { return guided_mode; }




















    const char *name() const override { return "GUIDED"; }
    const char *name4() const override { return "GUID"; }








    enum class Option : uint32_t {








    };




























};



class ModeGuidedCustom : public ModeGuided {






    Number mode_number() const override { return number; }

    const char *name() const override { return full_name; }
    const char *name4() const override { return short_name; }








};


class ModeGuidedNoGPS : public ModeGuided {




    Number mode_number() const override { return Number::GUIDED_NOGPS; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool is_autopilot() const override { return true; }



    const char *name() const override { return "GUIDED_NOGPS"; }
    const char *name4() const override { return "GNGP"; }



};


class ModeLand : public Mode {




    Number mode_number() const override { return Number::LAND; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return true; }

    bool is_landing() const override { return true; };



    AP_AdvancedFailsafe_Copter::control_mode afs_mode() const override { return AP_AdvancedFailsafe_Copter::control_mode::AFS_AUTO; }





    bool controlling_position() const { return control_position; }

    void set_land_pause(bool new_value) { land_pause = new_value; }



    const char *name() const override { return "LAND"; }
    const char *name4() const override { return "LAND"; }










};


class ModeLoiter : public Mode {




    Number mode_number() const override { return Number::LOITER; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool has_user_takeoff(bool must_navigate) const override { return true; }
    bool allows_autotune() const override { return true; }
    bool allows_auto_trim() const override { return true; }


    bool allows_inverted() const override { return true; };



    void set_precision_loiter_enabled(bool value) { _precision_loiter_enabled = value; }




    const char *name() const override { return "LOITER"; }
    const char *name4() const override { return "LOIT"; }



    float crosstrack_error_m() const override { return pos_control->crosstrack_error_m();}













};


class ModePosHold : public Mode {




    Number mode_number() const override { return Number::POSHOLD; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool has_user_takeoff(bool must_navigate) const override { return true; }
    bool allows_autotune() const override { return true; }
    bool allows_auto_trim() const override { return true; }



    const char *name() const override { return "POSHOLD"; }
    const char *name4() const override { return "PHLD"; }












    enum class RPMode {






    };










    struct {










    } brake;


















};


class ModeRTL : public Mode {




    Number mode_number() const override { return Number::RTL; }


    void run() override {

    }


    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return true; }

    bool requires_terrain_failsafe() const override { return true; }



    AP_AdvancedFailsafe_Copter::control_mode afs_mode() const override { return AP_AdvancedFailsafe_Copter::control_mode::AFS_AUTO; }












    enum class SubMode : uint8_t {






    };
    SubMode state() { return _state; }


    bool state_complete() const { return _state_complete; }






    enum class RTLAltType : int8_t {


    };




    const char *name() const override { return "RTL"; }
    const char *name4() const override { return "RTL "; }




    float crosstrack_error_m() const override { return wp_nav->crosstrack_error_m();}






    void set_descent_target_alt(uint32_t alt) { rtl_path.descent_target.alt = alt; }














    struct {






    } rtl_path;


    enum class ReturnTargetAltType {



    };







    enum class Options : int32_t {


    };

};


class ModeSmartRTL : public ModeRTL {




    Number mode_number() const override { return Number::SMART_RTL; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; }
    bool is_autopilot() const override { return true; }








    enum class SubMode : uint8_t {





    };



    const char *name() const override { return "SMARTRTL"; }
    const char *name4() const override { return "SRTL"; }





    float crosstrack_error_m() const override { return wp_nav->crosstrack_error_m();}

















};


class ModeSport : public Mode {




    Number mode_number() const override { return Number::SPORT; }




    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool has_user_takeoff(bool must_navigate) const override {

    }



    const char *name() const override { return "SPORT"; }
    const char *name4() const override { return "SPRT"; }



};


class ModeStabilize : public Mode {




    Number mode_number() const override { return Number::STABILIZE; }



    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return true; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }
    bool allows_save_trim() const override { return true; }
    bool allows_auto_trim() const override { return true; }
    bool allows_autotune() const override { return true; }
    bool allows_flip() const override { return true; }
    bool allows_entry_in_rc_failsafe() const override { return false; }



    const char *name() const override { return "STABILIZE"; }
    const char *name4() const override { return "STAB"; }



};


class ModeStabilize_Heli : public ModeStabilize {








    bool allows_inverted() const override { return true; };





};


class ModeSystemId : public Mode {



    Number mode_number() const override { return Number::SYSTEMID; }





    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return true; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };
    bool is_autopilot() const override { return false; }
    bool logs_attitude() const override { return true; }

    void set_magnitude(float input) { waveform_magnitude.set(input); }

    static const struct AP_Param::GroupInfo var_info[];





    const char *name() const override { return "SYSTEMID"; }
    const char *name4() const override { return "SYSI"; }






    enum class AxisType {




















    };



















    enum class SystemIDModeState {


    } systemid_state;
};

class ModeThrow : public Mode {




    Number mode_number() const override { return Number::THROW; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; };
    bool is_autopilot() const override { return false; }


    enum class ThrowType {


    };

    enum class PreThrowMotorState {


    };



    const char *name() const override { return "THROW"; }
    const char *name4() const override { return "THRW"; }









    enum ThrowModeStage {






    };







};


class ModeTurtle : public Mode {




    Number mode_number() const override { return Number::TURTLE; }





    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return true; }

    bool is_autopilot() const override { return false; }


    bool allows_entry_in_rc_failsafe() const override { return false; }


    const char *name() const override { return "TURTLE"; }
    const char *name4() const override { return "TRTL"; }












};





class ModeAvoidADSB : public ModeGuided {




    Number mode_number() const override { return Number::AVOID_ADSB; }




    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; }
    bool is_autopilot() const override { return true; }





    const char *name() const override { return "AVOID_ADSB"; }
    const char *name4() const override { return "AVOI"; }



};



class ModeFollow : public ModeGuided {





    Number mode_number() const override { return Number::FOLLOW; }





    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; }
    bool is_autopilot() const override { return true; }



    const char *name() const override { return "FOLLOW"; }
    const char *name4() const override { return "FOLL"; }







};


class ModeZigZag : public Mode {        






    Number mode_number() const override { return Number::ZIGZAG; }

    enum class Destination : uint8_t {


    };

    enum class Direction : uint8_t {




    } zigzag_direction;










    bool requires_GPS() const override { return true; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return true; }
    bool is_autopilot() const override { return true; }
    bool has_user_takeoff(bool must_navigate) const override { return true; }







    static const struct AP_Param::GroupInfo var_info[];



    const char *name() const override { return "ZIGZAG"; }
    const char *name4() const override { return "ZIGZ"; }





























    enum ZigZagState {



    } stage;

    enum AutoState {



    } auto_stage;







};


class ModeAutorotate : public Mode {





    Number mode_number() const override { return Number::AUTOROTATE; }




    bool is_autopilot() const override { return true; }
    bool requires_GPS() const override { return false; }
    bool has_manual_throttle() const override { return false; }
    bool allows_arming(AP_Arming::Method method) const override { return false; };

    static const struct AP_Param::GroupInfo  var_info[];



    const char *name() const override { return "AUTOROTATE"; }
    const char *name4() const override { return "AROT"; }






    enum class Phase {










    } current_phase;

};
