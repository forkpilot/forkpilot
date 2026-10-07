













































#define AP_MISSION_OPTIONS_DEFAULT          0       // Do not clear the mission when rebooting












/// @class    AP_Mission

class AP_Mission
{



    struct PACKED Jump_Command {


    };


    struct PACKED Conditional_Delay_Command {

    };


    struct PACKED Conditional_Distance_Command {

    };


    struct PACKED Yaw_Command {




    };


    struct PACKED Change_Speed_Command {



    };


    struct PACKED Set_Relay_Command {


    };


    struct PACKED Repeat_Relay_Command {



    };


    struct PACKED Set_Servo_Command {


    };


    struct PACKED Repeat_Servo_Command {




    };


    struct PACKED Mount_Control {



    };


    struct PACKED Digicam_Configure {







    };


    struct PACKED Digicam_Control {






    };


    struct PACKED Cam_Trigg_Distance {


    };


    struct PACKED Gripper_Command {


    };


    struct PACKED AuxFunction {


    };


    struct PACKED Altitude_Wait {



    };


    struct PACKED Guided_Limits_Command {




    };


    struct PACKED Do_VTOL_Transition {

    };


    struct PACKED Navigation_Delay_Command {




    };


    struct PACKED Do_Engine_Control {




    };


    struct PACKED Set_Yaw_Speed {



    };


    struct PACKED Winch_Command {




    };


    struct PACKED scripting_Command {



    };



    struct PACKED nav_script_time_Command_tag0 {




    };


    struct PACKED nav_script_time_Command {







    };



    struct PACKED nav_attitude_time_Command {





    };


    struct PACKED gimbal_manager_pitchyaw_Command {






    };


    struct PACKED image_start_capture_Command {




    };


    struct PACKED set_camera_zoom_Command {


    };


    struct PACKED set_camera_focus_Command {


    };


    struct PACKED set_camera_source_Command {



    };


    struct PACKED video_start_capture_Command {

    };


    struct PACKED video_stop_capture_Command {

    };

    union Content {



































































































        Location location{};      // Waypoint location
    };


    struct Mission_Command {













        bool operator ==(const Mission_Command &b) const { return (memcmp(this, &b, sizeof(Mission_Command)) == 0); }
        bool operator !=(const Mission_Command &b) const { return !operator==(b); }





        float get_loiter_turns(void) const {

            if (type_specific_bits & (1U<<1)) {


            }

        }
    };














    {

        if (_singleton != nullptr) {

        }




        AP_Param::setup_object_defaults(this, var_info);




    }



    {

    }





    enum mission_state {



    };










    {

    }




    {

    }


    uint16_t num_commands_max() const {

    }


















































    {

    }





    {

    }



    {

    }





    {

    }





    {

    }





    {

    }













    {

    }



    {

    }




































    {

    }




























    {

    }


    bool get_in_landing_sequence_flag() const {

    }


    bool get_in_return_path_flag() const {

    }



    {

    }


    bool is_resume() const { return _restart == 0 || _force_resume; }




    {

    }



















    enum class Option {



    };
    bool option_is_set(Option option) const {

    }


    bool continue_after_land(void) const {

    }


    static const struct AP_Param::GroupInfo var_info[];




















    bool is_valid_index(const uint16_t index) const { return index < _cmd_total; }


    bool failed_sdcard_storage(void) const {

    }









    struct {


    } _jump_tag;

    struct Mission_Flags {







    } _flags;































































































    struct Mission_Command  _nav_cmd;   // current "navigation" command.  It's position in the command list is held in _nav_cmd.index
    struct Mission_Command  _do_cmd;    // current "do" command.  It's position in the command list is held in _do_cmd.index
    struct Mission_Command  _resume_cmd;  // virtual wp command that is used to resume mission if the mission needs to be rewound on resume.






    struct jump_tracking_struct {


    } _jump_tracking[AP_MISSION_MAX_NUM_DO_JUMP_COMMANDS];










































};

namespace AP
{
AP_Mission *mission();
};
